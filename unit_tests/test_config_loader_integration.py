import os
import time
import unittest

import requests

from agileconfig_python import AgileConfigLoader
from agileconfig_python.config_loader import logger


WS_URL = 'ws://localhost:5000/ws'
APP_ID = 'test_app'
APP_SECRET = 'test_secret'
APP_ENV = 'DEV'


class TestAgileConfigLoader(unittest.TestCase):
    AGILE_BASE = 'http://localhost:5000'
    ADMIN_USER = 'admin'
    ADMIN_PASSWORD = 'admin123'
    BASELINE = {
        'database:host': 'db.example.com',
        'database:port': '5432',
        'api:key': 'test_api_key_12345',
    }

    _token = ''
    loader = None

    # --- AgileConfig admin helpers ---
    @classmethod
    def _admin_request(cls, method, path, **kwargs):
        r = requests.request(
            method,
            f'{cls.AGILE_BASE}{path}',
            headers={'Authorization': f'Bearer {cls._token}'},
            timeout=5,
            **kwargs,
        )
        r.raise_for_status()
        return r

    @classmethod
    def _existing_configs(cls):
        # Map key -> full editable config item (includes id) for upserts.
        r = cls._admin_request(
            'GET', '/Config/Search',
            params={'appId': APP_ID, 'env': APP_ENV},
        )
        data = r.json().get('data', [])
        return {item['key']: item for item in data}

    @classmethod
    def _seed(cls, entries):
        # Upsert so pre-existing keys are forced to the expected value
        # (AddRange alone silently skips duplicates), then publish once.
        existing = cls._existing_configs()
        for key, value in entries.items():
            if key in existing:
                item = dict(existing[key], value=value)
                cls._admin_request(
                    'POST', '/Config/Edit',
                    params={'env': APP_ENV}, json=item,
                )
            else:
                cls._admin_request(
                    'POST', '/Config/AddRange',
                    params={'env': APP_ENV},
                    json=[{'appId': APP_ID, 'key': key, 'value': value,
                           'group': '', 'comment': ''}],
                )
        cls._admin_request(
            'POST', '/Config/Publish',
            params={'env': APP_ENV},
            json={'appId': APP_ID, 'env': APP_ENV, 'log': 'unit test seed'},
        )

    @classmethod
    def setUpClass(cls):
        try:
            r = requests.post(
                f'{cls.AGILE_BASE}/admin/jwt/login',
                json={'userName': cls.ADMIN_USER,
                      'password': cls.ADMIN_PASSWORD},
                timeout=5,
            )
            r.raise_for_status()
            cls._token = r.json().get('token', '')
            if not cls._token:
                raise unittest.SkipTest('AgileConfig login failed')

            app = {'id': APP_ID, 'name': 'Test App', 'secret': APP_SECRET,
                   'enabled': True, 'envs': APP_ENV}
            cls._admin_request('POST', '/App/Add', json=app)
            cls._admin_request('POST', '/App/Edit', json=app)
            cls._seed(cls.BASELINE)
        except requests.exceptions.RequestException as exc:
            raise unittest.SkipTest(f'AgileConfig is not available: {exc}')

        AgileConfigLoader._instance = None
        cls.loader = AgileConfigLoader(WS_URL, APP_ID, APP_SECRET, APP_ENV)

    @classmethod
    def tearDownClass(cls):
        if cls.loader is not None:
            cls.loader.stop()
        AgileConfigLoader._instance = None

    def _reconfigure(self, url=WS_URL, app_id=APP_ID, start=True):
        # The loader is a singleton, so reconfigure in place rather than
        # constructing a new instance. stop() already clears cache/state.
        loader = self.loader
        loader.stop()
        loader._url = url
        loader._app_id = app_id
        loader._terminated = False
        loader.clear_cache()
        if start:
            loader.start()
        return loader

    def _wait_for(self, predicate, timeout=12, message=''):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        self.fail(message or 'Condition not met before timeout.')

    def setUp(self):
        self._reconfigure()

    def test_singleton_returns_same_instance(self):
        other = AgileConfigLoader('ws://127.0.0.1:59999/ws', 'x', 'y', 'T')
        self.assertIs(self.loader, other)

    def test_http_url_parser_translates_ws_and_wss(self):
        loader = self.loader
        loader.stop()
        try:
            loader._url = 'ws://localhost:5000/ws'
            self.assertEqual(loader._http_url_parser(), 'http://localhost:5000')
            loader._url = 'wss://config.example.com/ws'
            self.assertEqual(loader._http_url_parser(),
                             'https://config.example.com')
        finally:
            loader._url = WS_URL
            loader._terminated = False
            loader.start()

    def test_get_var_proxy_reads_latest_value(self):
        loader = self.loader
        loader._updated_event.set()
        loader._config_cache['service:timeout'] = '30'
        proxy = loader.get_var('timeout', prefix='service')
        self.assertEqual(str(proxy), '30')
        loader._config_cache['service:timeout'] = '60'
        self.assertEqual(str(proxy), '60')

    def test_get_var_value_reads_from_cache(self):
        loader = self.loader
        loader._updated_event.set()
        loader._config_cache['database:host'] = 'db.internal'
        self.assertEqual(
            loader.get_var_value('host', prefix='database'), 'db.internal')

    def test_fallback_reads_env_by_var_name_when_server_unavailable(self):
        loader = self._reconfigure(url='ws://127.0.0.1:59999/ws')
        os.environ['FALLBACK_TEST_VAR'] = 'fallback-value'
        try:
            value = loader.get_var_value('FALLBACK_TEST_VAR', prefix='ignored')
            self.assertIsNone(loader._ws_client)
            self.assertEqual(value, 'fallback-value')
            self.assertTrue(loader._use_os_env_fallback)
        finally:
            os.environ.pop('FALLBACK_TEST_VAR', None)

    def test_initial_bootstrap_populates_cache(self):
        loader = self.loader
        self._wait_for(
            lambda: 'database:host' in loader._config_cache,
            message='Cache did not populate from AgileConfig bootstrap.',
        )
        for key, value in self.BASELINE.items():
            self.assertEqual(loader._config_cache.get(key), value)

    def test_ws_reload_refreshes_cache_for_new_key(self):
        loader = self.loader
        self._wait_for(
            lambda: 'database:host' in loader._config_cache,
            message='Initial cache bootstrap did not complete.',
        )
        key = f'live:{int(time.time() * 1000)}'
        value = f'value_{int(time.time() * 1000)}'
        self._seed({key: value})
        self._wait_for(
            lambda: loader._config_cache.get(key) == value,
            message='Loader did not refresh cache after reload publish.',
        )

    def test_missing_url_or_app_id_skips_listener(self):
        loader = self._reconfigure(url='', app_id='', start=False)
        with self.assertLogs(logger, level='WARNING') as logs:
            loader.start()
        self.assertIsNone(loader._running_thread)
        self.assertTrue(
            any('not fully configured' in line for line in logs.output))

    def test_start_is_idempotent_when_thread_running(self):
        loader = self.loader
        first_thread = loader._running_thread
        self.assertIsNotNone(first_thread)
        loader.start()
        self.assertIs(loader._running_thread, first_thread)

    def test_stop_resets_state_and_clears_cache(self):
        loader = self.loader
        loader._config_cache['x:y'] = 'z'
        loader._updated_event.set()
        loader._use_os_env_fallback = True
        loader.stop()
        self.assertEqual(loader._config_cache, {})
        self.assertFalse(loader._updated_event.is_set())
        self.assertFalse(loader._use_os_env_fallback)
        self.assertIsNone(loader._running_thread)

    def test_restart_after_stop_rehydrates_cache(self):
        loader = self.loader
        self._wait_for(lambda: 'database:host' in loader._config_cache)
        loader.stop()
        loader._terminated = False
        loader.start()
        self._wait_for(
            lambda: 'database:host' in loader._config_cache,
            message='Cache did not rehydrate after restart.',
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
