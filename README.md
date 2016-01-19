# Homu

Homu is one of the [main characters] in Puella Magi Madoka Magica.

Homu is also [a village in Iran] having 226 residents.

Homu is also a bot that integrates with GitHub and your favorite
continuous integration service, such as [Solano], [Jenkins], [Buildbot],
or [Travis CI].

[main characters]: https://wiki.puella-magi.net/Homura_Akemi
[a village in Iran]: https://en.wikipedia.org/wiki/Homu
[Solano]: https://www.solanolabs.com/
[Jenkins]: https://jenkins-ci.org/
[Buildbot]: http://buildbot.net/
[Travis CI]: https://travis-ci.org/

## Why is it needed?

Let's take Travis CI as an example. If you send a pull request to a repository,
Travis CI instantly shows you the test result, which is great. However, after
several other pull requests are merged into the `master` branch, your pull
request can *still* break things after being merged into `master`. The
traditional continuous integration solutions don't protect you from this.

In fact, that's why they provide the build status badges. If anything pushed to
`master` is completely free from any breakage, those badges will **not** be
necessary, as they will always be green. The badges themselves prove that there
can still be some breakages, even when continuous integration services are used.

To solve this problem, the test procedure should be executed *just before the
merge*, not just after the pull request is received. You can manually click the
"restart build" button each time before you merge a pull request, but Homu can
automate this process. It listens to the pull request comments, waiting for an
approval comment from one of the configured reviewers. When the pull request is
approved, Homu tests it using your favorite continuous integration service, and
only when it passes all the tests, it is merged into `master`.

Note that Homu is **not** a replacement of Travis CI or Buildbot. It works on
top of them. Homu itself doesn't have the ability to test pull requests.

## Influences of bors

Homu is largely inspired by [bors]. The concept of "tests should be done just
before the merge" came from bors. However, there are also some differences:

1. Stateful: Unlike bors, which intends to be stateless, Homu is stateful. It
   means that Homu does not need to retrieve all the information again and again
   from GitHub at every run. This is essential because of the GitHub's rate
   limiting. Once it downloads the initial state, the following changes are
   delivered with the [Webhooks] API.
2. Pushing over polling: Homu prefers pushing wherever possible. The pull
   requests from GitHub are retrieved using Webhooks, as stated above. The test
   results from Buildbot are pushed back to Homu with the [HttpStatusPush]
   feature. This approach improves the overall performance and the response
   time, because the bot is informed about the status changes immediately.

And also, Homu has more features, such as `rollup`, `try`, and the Travis CI
support.

[bors]: https://github.com/graydon/bors
[Webhooks]: https://developer.github.com/webhooks/
[HttpStatusPush]: http://docs.buildbot.net/current/manual/cfg-statustargets.html#httpstatuspush

## Usage

### How to install

```sh
pyvenv .venv
. .venv/bin/activate

# Stable version

pip install homu

# Development version

git clone https://github.com/coupa/homu.git
pip install -e homu

# Dependencies not explicitly required in the package at the moment

cat requirements.txt                                                                                                                                   master
-e git+git://github.com/coupa/homu.git#egg=homu
--allow-external mysql-connector-python
mysql-connector-python
github3.py
PyYAML==3.11

pip install -r requirements.txt
```

### How to configure

1. Copy `cfg.sample.toml` to `cfg.toml`, and edit it accordingly.

2. Create a GitHub account that will be used by Homu. You can also use an
   existing account. In the [account settings][settings], register a new
   application and generate a new access token (with the `repo` permission).
   The OAuth Callback URL should be `http://HOST:PORT/callback`, the homepage URL
   isn't needed and can be anything, for example `http://HOST:PORT/`.

3. Add a Webhook to your repository:

 - Payload URL: `http://HOST:PORT/github`
 - Content type: `application/json`
 - Secret: The same as `repo.NAME.github.secret` in cfg.toml
 - Events: Issue Comment, Pull Request, Push

4. Add a Webhook to your continuous integration service:

 - Solano

  A post_build script is required to post commit, success, url,
  and hmac. At Coupa we use script/solano/post_build.sh

 - Jenkins

  A script is required to post commit, success, url, and hmac.
  At Coupa we use devscripts/jenkins/ci-scripts/report-to-merge-bot.sh

 - Buildbot

   Insert the following code to the `master.cfg` file:

    ```python
    from buildbot.status.status_push import HttpStatusPush

    c['status'].append(HttpStatusPush(
        serverUrl='http://HOST:PORT/buildbot',
        extra_post_params={'secret': 'repo.NAME.buildbot.secret in cfg.toml'},
    ))
    ```

 - Travis CI

   Add [your Travis token][travis] as `repo.NAME.travis.token` in cfg.toml.
   Insert the following code to the `.travis.yml` file:

    ```yaml
    notifications:
        webhooks: http://HOST:PORT/travis

    branches:
        only:
            - auto
    ```

[settings]: https://github.com/settings/applications
[travis]: https://travis-ci.org/profile/info

### How to run

```sh
. .venv/bin/activate

homu
```
