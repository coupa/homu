from contextlib import contextmanager
from mysql.connector.pooling import MySQLConnectionPool
from mysql.connector.errors import PoolError
import threading
import time
import yaml


class Singleton(type):
    def __init__(cls, *args, **kwargs):
        cls.__lock = threading.Lock()
        cls.__instance = None
    def __call__(cls, *args, **kwargs):
        if cls.__instance is None:
            with cls.__lock:
                if cls.__instance is None:
                    cls.__instance = super().__call__(*args, **kwargs)
        return cls.__instance


class Database(object, metaclass=Singleton):
    def __init__(self):
        cfg = self.__get_cfg()
        self.pool = MySQLConnectionPool(pool_name='dbpool',
                                        pool_size=5,
                                        **cfg)

    def __get_cfg(self):
        with open('database.yml') as f:
            cfg = yaml.load(f.read())['production']

        keys = ['username', 'password', 'host', 'port', 'database', 'ssl_ca',
                'ssl_cert', 'ssl_key']

        no_ = lambda s: s.replace('_', '')
        cfg = { k: cfg.get(no_(k)) for k in keys if no_(k) in cfg }
        cfg['ssl_verify_cert'] = True
        # Mysql documentation incorrectly says 'username' is an alias of 'user'.
        cfg['user'] = cfg.pop('username')
        return cfg

    @contextmanager
    def get_connection(self):
        def get_conn(attempt=0):
            try:
                return self.pool.get_connection()
            except PoolError:
                if 20 > attempt:
                    time.sleep(0.2)
                    return get_conn(attempt + 1)
                else:
                    raise
        connection = get_conn()
        yield connection
        # connection.close() will fail with an unread_result.
        if connection._cnx.unread_result:
            connection._cnx.get_rows()
        connection.close()

    def close_all(self):
        self.pool.reset_session()
