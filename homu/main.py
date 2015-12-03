import argparse
from datetime import datetime, timezone
import github3
import os
import toml
import json
import re
from .database import Database
from . import utils
import logging
from threading import Thread, Lock
import time
import traceback
import requests
from contextlib import contextmanager
from functools import partial
from itertools import chain
from queue import Queue

STATUS_TO_PRIORITY = {
    'success': 0,
    'pending': 1,
    'approved': 2,
    '': 3,
    'error': 4,
    'failure': 5,
}

INTERRUPTED_BY_HOMU_FMT = 'Interrupted by Homu ({})'
INTERRUPTED_BY_HOMU_RE = re.compile(r'Interrupted by Homu \((.+?)\)')

@contextmanager
def buildbot_sess(repo_cfg):
    sess = requests.Session()

    sess.post(repo_cfg['buildbot']['url'] + '/login', allow_redirects=False, data={
        'username': repo_cfg['buildbot']['username'],
        'passwd': repo_cfg['buildbot']['password'],
    })

    yield sess

    sess.get(repo_cfg['buildbot']['url'] + '/logout', allow_redirects=False)

class PullReqState:
    num = 0
    priority = 0
    rollup = False
    title = ''
    body = ''
    head_ref = ''
    base_ref = ''
    assignee = ''

    def __init__(self, num, head_sha, status, repo_label, mergeable_que, gh,
                 owner, name, repos):
        self.head_advanced('', use_db=False)

        self.num = num
        self.head_sha = head_sha
        self.status = status
        self.repo_label = repo_label
        self.mergeable_que = mergeable_que
        self.gh = gh
        self.owner = owner
        self.name = name
        self.repos = repos

        self.db = Database()

    def head_advanced(self, head_sha, *, use_db=True):
        self.head_sha = head_sha
        self.approved_by = ''
        self.status = ''
        self.merge_sha = ''
        self.build_res = {}
        self.try_ = False
        self.mergeable = None

        if use_db:
            self.set_status('')
            self.set_mergeable(None)
            self.init_build_res([])

    def __repr__(self):
        return 'PullReqState:{}/{}#{}(approved_by={}, priority={}, status={})'.format(
            self.owner,
            self.name,
            self.num,
            self.approved_by,
            self.priority,
            self.status,
        )

    def sort_key(self):
        return [
            STATUS_TO_PRIORITY.get(self.get_status(), -1),
            1 if self.mergeable is False else 0,
            0 if self.approved_by else 1,
            1 if self.rollup else 0,
            -self.priority,
            self.num,
        ]

    def __lt__(self, other):
        return self.sort_key() < other.sort_key()

    def add_comment(self, text):
        issue = getattr(self, 'issue', None)
        if not issue:
            issue = self.issue = self.get_repo().issue(self.num)

        issue.create_comment(text)

    def set_status(self, status):
        self.status = status

        sql = 'UPDATE pull SET status = %s WHERE repo = %s AND num = %s'
        with self.db.get_connection() as db_conn:
            db_conn.cursor().execute(sql, [self.status, self.repo_label,
                                           self.num])
            db_conn.commit()

            # FIXME: self.try_ should also be saved in the database
            if not self.try_:
                sql = 'UPDATE pull SET merge_sha = %s WHERE repo = %s AND num = %s'
                db_conn.cursor().execute(sql, [self.merge_sha, self.repo_label,
                                               self.num])
                db_conn.commit()

    def get_status(self):
        return 'approved' if self.status == '' and self.approved_by and self.mergeable is not False else self.status

    def set_mergeable(self, mergeable, *, cause=None, que=True):
        if mergeable is not None:
            self.mergeable = mergeable

            sql = 'REPLACE INTO mergeable (repo, num, mergeable) ' \
                  'VALUES (%s, %s, %s)'
            with self.db.get_connection() as db_conn:
                db_conn.cursor().execute(sql, [self.repo_label, self.num,
                                               self.mergeable])
                db_conn.commit()
        else:
            if que:
                self.mergeable_que.put([self, cause])
            else:
                self.mergeable = None

            with self.db.get_connection() as db_conn:
                sql = 'DELETE FROM mergeable WHERE repo = %s AND num = %s'
                db_conn.cursor().execute(sql, [self.repo_label, self.num])
                db_conn.commit()

    def init_build_res(self, builders, *, use_db=True):
        self.build_res = {x: {
            'res': None,
            'url': '',
        } for x in builders}

        if use_db:
            with self.db.get_connection() as db_conn:
                sql = 'DELETE FROM build_res WHERE repo = %s AND num = %s'
                db_conn.cursor().execute(sql, [self.repo_label, self.num])
                db_conn.commit()

    def set_build_res(self, builder, res, url):
        if builder not in self.build_res:
            raise Exception('Invalid builder: {}'.format(builder))

        self.build_res[builder] = {
            'res': res,
            'url': url,
        }

        with self.db.get_connection() as db_conn:
            db_conn.cursor().execute('REPLACE INTO build_res ' \
                                     '(repo, num, builder, res, url, ' \
                                     'merge_sha) VALUES ' \
                                     '(%s, %s, %s, %s, %s, %s)',
                                     [self.repo_label, self.num, builder, res,
                                      url, self.merge_sha])
            db_conn.commit()

    def build_res_summary(self):
        return ', '.join('{}: {}'.format(builder, data['res'])
                         for builder, data in self.build_res.items())

    def get_repo(self):
        repo = self.repos[self.repo_label]
        if not repo:
            self.repos[self.repo_label] = repo = self.gh.repository(self.owner, self.name)

            assert repo.owner.login == self.owner
            assert repo.name == self.name
        return repo

    def save(self):
        with self.db.get_connection() as db_conn:
            db_conn.cursor().execute('REPLACE INTO pull ' \
                                     '(repo, num, status, merge_sha, title, ' \
                                     'body, head_sha, head_ref, base_ref, ' \
                                     'assignee, approved_by, priority, ' \
                                     'try_, rollup) VALUES (%s, %s, %s, %s, ' \
                                     '%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                                     [self.repo_label, self.num, self.status,
                                      self.merge_sha, self.title, self.body,
                                      self.head_sha, self.head_ref,
                                      self.base_ref, self.assignee,
                                      self.approved_by, self.priority,
                                      self.try_, self.rollup])
            db_conn.commit()

    def refresh(self):
        issue = self.get_repo().issue(self.num)

        self.title = issue.title
        self.body = issue.body

def sha_cmp(short, full):
    return len(short) >= 4 and short == full[:len(short)]

def sha_or_blank(sha):
    return sha if re.match(r'^[0-9a-f]+$', sha) else ''

def parse_commands(body, username, repo_cfg, state, my_username, *,
                   realtime=False, sha=''):
    if username not in repo_cfg['reviewers'] and username != my_username:
        return False

    state_changed = False

    words = list(chain.from_iterable(re.findall(r'\S+', x) for x in body.splitlines() if '@' + my_username in x))
    for i, word in reversed(list(enumerate(words))):
        found = True

        if word == 'r+' or word.startswith('r='):
            if not sha and i+1 < len(words):
                cur_sha = sha_or_blank(words[i+1])
            else:
                cur_sha = sha

            approver = word[len('r='):] if word.startswith('r=') else username

            if sha_cmp(cur_sha, state.head_sha):
                state.approved_by = approver

                state.save()
            elif realtime and username != my_username:
                if cur_sha:
                    msg = '`{}` is not a valid commit SHA.'.format(cur_sha)
                    state.add_comment(':question: {} Please try again with '
                                      '`{:.7}`.'.format(msg, state.head_sha))
                else:
                    state.add_comment(':pushpin: Commit {:.7} has been approved by `{}`\n\n<!-- @{} r={} {} -->'.format(state.head_sha, approver, my_username, approver, state.head_sha))

        elif word == 'r-':
            state.approved_by = ''

            state.save()

        elif word.startswith('p='):
            try: state.priority = int(word[len('p='):])
            except ValueError: pass

            state.save()

        elif word == 'retry' and realtime:
            state.set_status('')

        elif word in ['try', 'try-'] and realtime:
            state.try_ = word == 'try'

            state.merge_sha = ''
            state.init_build_res([])

            state.save()

        elif word in ['rollup', 'rollup-']:
            state.rollup = word == 'rollup'

            state.save()

        elif word == 'force' and realtime:
            with buildbot_sess(repo_cfg) as sess:
                res = sess.post(repo_cfg['buildbot']['url'] + '/builders/_selected/stopselected', allow_redirects=False, data={
                    'selected': repo_cfg['buildbot']['builders'],
                    'comments': INTERRUPTED_BY_HOMU_FMT.format(int(time.time())),
                })

            if 'authzfail' in res.text:
                err = 'Authorization failed'
            else:
                mat = re.search('(?s)<div class="error">(.*?)</div>', res.text)
                if mat:
                    err = mat.group(1).strip()
                    if not err: err = 'Unknown error'
                else:
                    err = ''

            if err:
                state.add_comment(':bomb: Buildbot returned an error: `{}`'.format(err))

        elif word == 'clean' and realtime:
            state.merge_sha = ''
            state.init_build_res([])

            state.save()

        else:
            found = False

        if found:
            state_changed = True

            words[i] = ''

    return state_changed

def create_merge(state, repo_cfg, branch):
    base_sha = state.get_repo().ref('heads/' + state.base_ref).object.sha
    utils.github_set_ref(
        state.get_repo(),
        'heads/' + branch,
        base_sha,
        force=True,
    )

    state.refresh()

    merge_msg = 'Auto merge of #{} - {}, r={}\n\n{}\n\n{}'.format(
        state.num,
        state.head_ref,
        '<try>' if state.try_ else state.approved_by,
        state.title,
        state.body,
    )
    try: merge_commit = state.get_repo().merge(branch, state.head_sha, merge_msg)
    except github3.models.GitHubError as e:
        if e.code != 409: raise

        state.set_status('error')
        desc = 'Merge conflict'
        utils.github_create_status(state.get_repo(), state.head_sha, 'error', '', desc, context='homu')

        state.add_comment(':lock: ' + desc)

        return None

    return merge_commit

def start_build(state, repo_cfgs, buildbot_slots, logger):
    if buildbot_slots[0]:
        return True

    assert state.head_sha == state.get_repo().pull_request(state.num).head.sha

    repo_cfg = repo_cfgs[state.repo_label]

    if 'buildbot' in repo_cfg:
        branch = 'try' if state.try_ else 'auto'
        branch = repo_cfg.get('branch', {}).get(branch, branch)
        builders = repo_cfg['buildbot']['try_builders' if state.try_ else 'builders']
    elif 'travis' in repo_cfg:
        branch = repo_cfg.get('branch', {}).get('auto', 'auto')
        builders = ['travis']
    elif 'status' in repo_cfg:
        branch = repo_cfg.get('branch', {}).get('auto', 'auto')
        builders = ['status']
    elif 'testrunners' in repo_cfg:
        branch = 'merge_bot_{}'.format(state.base_ref)
        builders = repo_cfg['testrunners'].get('builders', [])
    else:
        raise RuntimeError('Invalid configuration')

    merge_commit = create_merge(state, repo_cfg, branch)
    if not merge_commit:
        return False

    state.init_build_res(builders)
    state.merge_sha = merge_commit.sha

    state.save()

    if 'buildbot' in repo_cfg:
        buildbot_slots[0] = state.merge_sha

    logger.info('Starting build of {}/{}#{} on {}: {}'.format(state.owner,
                                                              state.name,
                                                              state.num,
                                                              branch,
                                                              state.merge_sha))

    state.set_status('pending')
    desc = '{} commit {:.7} with merge {:.7}...'.format('Trying' if state.try_ else 'Testing', state.head_sha, state.merge_sha)
    github_create_status = partial(utils.github_create_status,
                                   repo=state.get_repo(),
                                   sha=state.head_sha,
                                   state='pending',
                                   description=desc)
    if 'testrunners' in repo_cfg:
        for builder in builders:
            github_create_status(context='merge-test/{}'.format(builder))
    else:
        github_create_status(context='homu')

    state.add_comment(':hourglass: ' + desc)

    return True

def start_rebuild(state, repo_cfgs):
    repo_cfg = repo_cfgs[state.repo_label]

    if 'buildbot' not in repo_cfg or not state.build_res:
        return False

    builders = []
    succ_builders = []

    for builder, info in state.build_res.items():
        if not info['url']:
            return False

        if info['res']:
            succ_builders.append([builder, info['url']])
        else:
            builders.append([builder, info['url']])

    if not builders or not succ_builders:
        return False

    base_sha = state.get_repo().ref('heads/' + state.base_ref).object.sha
    parent_shas = [x['sha'] for x in state.get_repo().commit(state.merge_sha).parents]

    if base_sha not in parent_shas:
        return False

    utils.github_set_ref(state.get_repo(), 'tags/homu-tmp', state.merge_sha, force=True)

    builders.sort()
    succ_builders.sort()

    with buildbot_sess(repo_cfg) as sess:
        for builder, url in builders:
            res = sess.post(url + '/rebuild', allow_redirects=False, data={
                'useSourcestamp': 'exact',
                'comments': 'Initiated by Homu',
            })

            if 'authzfail' in res.text:
                err = 'Authorization failed'
            elif builder in res.text:
                err = ''
            else:
                mat = re.search('<title>(.+?)</title>', res.text)
                err = mat.group(1) if mat else 'Unknown error'

            if err:
                state.add_comment(':bomb: Failed to start rebuilding: `{}`'.format(err))
                return False

    state.set_status('pending')

    msg_1 = 'Previous build results'
    msg_2 = ' for {}'.format(', '.join('[{}]({})'.format(builder, url) for builder, url in succ_builders))
    msg_3 = ' are reusable. Rebuilding'
    msg_4 = ' only {}'.format(', '.join('[{}]({})'.format(builder, url) for builder, url in builders))

    utils.github_create_status(state.get_repo(), state.head_sha, 'pending', '', '{}{}...'.format(msg_1, msg_3), context='homu')

    state.add_comment(':zap: {}{}{}{}...'.format(msg_1, msg_2, msg_3, msg_4))

    return True

def start_build_or_rebuild(state, repo_cfgs, *args):
    if start_rebuild(state, repo_cfgs):
        return True

    return start_build(state, repo_cfgs, *args)

def process_queue(states, repos, repo_cfgs, logger, buildbot_slots):
    for repo_label, repo in repos.items():
        repo_states = sorted(states[repo_label].values())

        for state in repo_states:
            if state.status == 'pending' and not state.try_:
                break

            elif state.status == '' and state.approved_by:
                if start_build_or_rebuild(state, repo_cfgs, buildbot_slots, logger):
                    return

            elif state.status == 'success' and state.try_ and state.approved_by:
                state.try_ = False

                state.save()

                if start_build(state, repo_cfgs, buildbot_slots, logger):
                    return

        for state in repo_states:
            if state.status == '' and state.try_:
                if start_build(state, repo_cfgs, buildbot_slots, logger):
                    return

def fetch_mergeability(mergeable_que):
    re_pull_num = re.compile('(?i)merge (?:of|pull request) #([0-9]+)')

    while True:
        try:
            state, cause = mergeable_que.get()

            pr = state.get_repo().pull_request(state.num)
            if pr is None:
                time.sleep(5)
                pr = state.get_repo().pull_request(state.num)
            mergeable = pr.mergeable
            if mergeable is None:
                time.sleep(5)
                mergeable = pr.mergeable

            if state.mergeable is True and mergeable is False:
                if cause:
                    mat = re_pull_num.search(cause['title'])

                    if mat: issue_or_commit = '#' + mat.group(1)
                    else: issue_or_commit = cause['sha'][:7]
                    issue_or_commit = \
                        ' (presumably {})'.format(issue_or_commit)
                else:
                    issue_or_commit = ''

                state.add_comment(':x: The latest upstream changes{} made '
                    'this pull request unmergeable. Please resolve the merge '
                    'conflicts.'.format(issue_or_commit))

            state.set_mergeable(mergeable, que=False)

        except:
            traceback.print_exc()

        finally:
            mergeable_que.task_done()

def synchronize(repo_label, repo_cfg, logger, gh, states, repos, mergeable_que,
                my_username, repo_labels):
    logger.info('Synchronizing {}...'.format(repo_label))
    db = Database()

    repo = gh.repository(repo_cfg['owner'], repo_cfg['name'])

    with db.get_connection() as db_conn:
        for tbl in ['pull', 'build_res', 'mergeable']:
            sql = 'DELETE FROM {} WHERE repo = %s'.format(tbl)
            db_conn.cursor().execute(sql, [repo_label])
        db_conn.commit()

        states[repo_label] = {}
        repos[repo_label] = repo

    for pull in repo.iter_pulls(state='open'):
        # Ignore PRs older than about two months.
        update_delta = datetime.now(timezone.utc) - pull.updated_at
        if 5e6 < update_delta.total_seconds():
            logger.debug('Ignoring PR for merge {} because it has not ' \
                         'been updated since {}.'.format(pull.merge_commit_sha,
                                                         pull.updated_at))
            continue

        with db.get_connection() as db_conn:
            cursor = db_conn.cursor()
            sql = 'SELECT status FROM pull WHERE repo = %s AND num = %s'
            cursor.execute(sql, [repo_label, pull.number])
            row = cursor.fetchone()
            if row:
                status = row[0]
            else:
                status = ''
                for info in utils.github_iter_statuses(repo, pull.head.sha):
                    if info.context == 'homu':
                        status = info.state
                        break

        state = PullReqState(pull.number, pull.head.sha, status, repo_label,
                             mergeable_que, gh, repo_cfg['owner'],
                             repo_cfg['name'], repos)
        state.title = pull.title
        state.body = pull.body
        state.head_ref = pull.head.repo[0] + ':' + pull.head.ref
        state.base_ref = pull.base.ref
        state.set_mergeable(None)
        state.assignee = pull.assignee.login if pull.assignee else ''

        for comment in pull.iter_comments():
            if comment.original_commit_id == pull.head.sha:
                parse_commands(
                    comment.body,
                    comment.user.login,
                    repo_cfg,
                    state,
                    my_username,
                    sha=comment.original_commit_id,
                )

        for comment in pull.iter_issue_comments():
            parse_commands(
                comment.body,
                comment.user.login,
                repo_cfg,
                state,
                my_username,
            )

        state.save()

        states[repo_label][pull.number] = state

    logger.info('Done synchronizing {}!'.format(repo_label))

    logger.debug('Github rate limit status: {}'.format(gh.rate_limit()))

def arguments():
    parser = argparse.ArgumentParser(description =
                                     'A bot that integrates with GitHub and '
                                     'your favorite continuous integration service')
    parser.add_argument('-v', '--verbose',
                        action='store_true', help='Enable more verbose logging')

    return parser.parse_args()

def main():
    args = arguments()

    logger = logging.getLogger('homu')
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logger.addHandler(logging.StreamHandler())

    try:
        with open('cfg.toml') as fp:
            cfg = toml.loads(fp.read())
    except FileNotFoundError:
        with open('cfg.json') as fp:
            cfg = json.loads(fp.read())

    gh = github3.login(token=cfg['github']['access_token'])

    rate_limit = gh.rate_limit()
    logger.debug('Github rate limit status: {}'.format(rate_limit))
    if not rate_limit['rate']['remaining']:
        reset_time = datetime.fromtimestamp(rate_limit['rate']['reset'])
        logger_msg = 'Github rate limit exhausted! Sleeping until {}'
        logger.info(logger_msg.format(reset_time.isoformat()))
        reset_delta = reset_time - datetime.now()
        time.sleep(reset_delta.total_seconds())

    states = {}
    repos = {}
    repo_cfgs = {}
    buildbot_slots = ['']
    my_username = gh.user().login
    repo_labels = {}
    mergeable_que = Queue()

    db = Database()
    with db.get_connection() as db_conn:
        schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
        schema = open(schema_path).read()
        # execute with multi=True requires enumeration.
        list(db_conn.cursor().execute(multi=True, operation=schema))

        for repo_label, repo_cfg in cfg['repo'].items():
            repo_cfgs[repo_label] = repo_cfg
            repo_labels[repo_cfg['owner'], repo_cfg['name']] = repo_label

            repo_states = {}
            repos[repo_label] = None

            cursor = db_conn.cursor()
            cursor.execute('SELECT num, head_sha, status, title, body, ' \
                           'head_ref, base_ref, assignee, approved_by, ' \
                           'priority, try_, rollup, merge_sha FROM pull ' \
                           'WHERE repo = %s', [repo_label])
            for (num, head_sha, status, title, body, head_ref, base_ref,
                    assignee, approved_by, priority, try_, rollup,
                    merge_sha) in cursor.fetchall():
                state = PullReqState(num, head_sha, status, repo_label,
                                     mergeable_que, gh, repo_cfg['owner'],
                                     repo_cfg['name'], repos)
                state.title = title
                state.body = body
                state.head_ref = head_ref
                state.base_ref = base_ref
                state.set_mergeable(None)
                state.assignee = assignee

                state.approved_by = approved_by
                state.priority = int(priority)
                state.try_ = bool(try_)
                state.rollup = bool(rollup)

                if merge_sha:
                    if 'buildbot' in repo_cfg:
                        builders = repo_cfg['buildbot']['builders']
                    elif 'travis' in repo_cfg:
                        builders = ['travis']
                    elif 'status' in repo_cfg:
                        builders = ['status']
                    elif 'testrunners' in repo_cfg:
                        builders = repo_cfg['testrunners'].get('builders', [])
                    else:
                        raise RuntimeError('Invalid configuration')

                    state.init_build_res(builders, use_db=False)
                    state.merge_sha = merge_sha

                elif state.status == 'pending':
                    # FIXME: There might be a better solution
                    state.status = ''

                    state.save()

                repo_states[num] = state

            states[repo_label] = repo_states

        cursor = db_conn.cursor()
        cursor.execute('SELECT repo, num, builder, res, url, merge_sha FROM build_res')
        for repo_label, num, builder, res, url, merge_sha in cursor.fetchall():
            try:
                state = states[repo_label][num]
                if builder not in state.build_res: raise KeyError
                if state.merge_sha != merge_sha: raise KeyError
            except KeyError:
                cursor = db_conn.cursor()
                cursor.execute('DELETE FROM build_res WHERE repo = %s AND ' \
                               'num = %s AND builder = %s',
                               [repo_label, num, builder])
                db_conn.commit()
                continue

            state.build_res[builder] = {
                'res': bool(res) if res is not None else None,
                'url': url,
            }

        cursor = db_conn.cursor()
        cursor.execute('SELECT repo, num, mergeable FROM mergeable')
        for repo_label, num, mergeable in cursor.fetchall():
            try: state = states[repo_label][num]
            except KeyError:
                cursor = db_conn.cursor()
                cursor.execute('DELETE FROM mergeable WHERE repo = %s AND ' \
                               'num = %s', [repo_label, num])
                db_conn.commit()
                continue

            state.mergeable = bool(mergeable) if mergeable is not None else None

        queue_handler_lock = Lock()
        def queue_handler():
            with queue_handler_lock:
                return process_queue(states, repos, repo_cfgs, logger,
                                     buildbot_slots)

        from . import server
        Thread(target=server.start, args=[cfg, states, queue_handler, repo_cfgs,
                                          repos, logger, buildbot_slots,
                                          my_username, repo_labels,
                                          mergeable_que, gh]).start()

        Thread(target=fetch_mergeability, args=[mergeable_que]).start()


        for repo_label, repo_cfg in cfg['repo'].items():
            t = Thread(target=synchronize,
                       args=[repo_label, repo_cfg, logger, gh, states, repos,
                             mergeable_que, my_username, repo_labels])
            t.start()

        queue_handler()

if __name__ == '__main__':
    main()
