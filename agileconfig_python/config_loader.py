import json
import logging
import os
import threading
import time
from base64 import b64encode
from typing import Optional, Dict, Any

import requests
from websockets import ConnectionClosedError
from websockets.sync.client import connect as ws_connect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

AGILE_CONFIG_TIMEOUT = 20


class ConfigStrWithUpdate:
    def __init__(self, agile_config_loader, prefix: str, var_name : str):
        self.__prefix = prefix
        self.__var_name = var_name
        self.__agile_config_loader = agile_config_loader

    def __str__(self):
        return self.__agile_config_loader.get_var_value(self.__var_name, self.__prefix)


class AgileConfigLoader:
    """
    Loads configuration from AgileConfig server via WebSocket.
    Falls back to environment variables if AgileConfig is unavailable.

    Singleton that starts background WebSocket listening on instantiation.

    Requires environment variables:
    - AGILE_CONFIG_URL: WebSocket URL (e.g., "ws://localhost:5000/ws" or "wss://config.example.com/ws")
    - AGILE_CONFIG_APP_ID: Application ID for AgileConfig
    - AGILE_CONFIG_SECRET: Secret key for AgileConfig
    - AGILE_CONFIG_ENV: Environment (default: "DEV")
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        """Singleton pattern: ensure only one instance exists."""
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, url: str, app_id: str, secret: str, env: str):
        """Initialize AgileConfigLoader from environment variables and start background listening."""
        # Only initialize once
        if not hasattr(self, '_initialized'):
            self._initialized = True

            self._url = url
            self._app_id = app_id
            self._secret = secret
            self._env = env

            self._ws_client: Optional[Any] = None
            self._config_cache: Dict[str, Any] = {}
            self._ready = False
            self._terminated = False
            self._headers: Optional[Dict[str, Any]] = None
            self._running_thread = None

            self._updated_event = threading.Event()
            self._use_os_env_fallback = False

            # Pre-compute auth headers (doesn't change across retries)
            auth_string = f"{self._app_id}:{self._secret}"
            auth_bytes = b64encode(auth_string.encode()).decode()
            self._headers = {
                "appid": self._app_id,
                "env": self._env,
                "Authorization": f"Basic {auth_bytes}",
            }
            self.start()

            return

    def _http_url_parser(self) -> str:
        """Derive the HTTP base URL from the configured WebSocket URL."""
        url = self._url
        if url.startswith("wss://"):
            url = "https://" + url[len("wss://"):]
        elif url.startswith("ws://"):
            url = "http://" + url[len("ws://"):]
        if url.endswith("/ws"):
            url = url[: -len("/ws")]
        return url.rstrip("/")

    def _get_config_from_server(self):
        url = f'{self._http_url_parser()}/api/config/app/{self._app_id}'
        r = requests.get(url, headers=self._headers, params={'env': self._env}, timeout=10,)
        r.raise_for_status()
        configs = {
            f'{_["group"]}:{_["key"]}' if _["group"] else _["key"]: _['value']
            for _ in r.json()
        }
        self._config_cache = configs

    def _start_config_listener(self):
        while not self._terminated:
            self._ready = False
            try:
                self._get_config_from_server()
                self._updated_event.set()
                logger.info(f'Successfully retrieved config from AgileConfig server at {self._url}')
                self._ready = True
            except requests.exceptions.HTTPError as e:
                logger.warning(
                    f'Failed to retrieve config from server with error {e.__repr__()}. Will retry in 5 seconds.'
                )
            except Exception as e:
                logger.warning(
                    f'Failed to retrieve config from server with unexpected error {e.__repr__()}. '
                    f'Will retry in 5 seconds.'
                )
            if not self._ready:
                time.sleep(5)
                continue

            try:
                with ws_connect(
                    self._url,
                    additional_headers=self._headers,
                    ping_interval=30,
                ) as client:
                    self._ws_client = client
                    if not client.ping().wait(timeout=5):
                        raise TimeoutError('Initial ping to AgileConfig server timed out!')
                    logger.info(f"Successfully connected to AgileConfig ws at {self._url}")
                    for msg in client:
                        logger.info(f'Received message {msg} from {self._url}')
                        if json.loads(msg)['Action'] == 'reload':
                            self._get_config_from_server()
                            logger.info('Successfully updated config.')
            except TimeoutError as e:
                logger.warning(f'Encountered timeout error: {e.__repr__()}. Will retry in 5 seconds.')
            except requests.exceptions.HTTPError as e:
                logger.warning(
                    f'Failed to retrieve config from server with error {e.__repr__()}. Will retry in 5 seconds.'
                )
            except ConnectionClosedError:
                if self._terminated:
                    logger.info('Connection closed due to termination. Exiting.')
                    return
                logger.warning(f'Connection unexpectedly closed. Will retry in 5 seconds.')
            except Exception as e:
                logger.warning(
                    f'Encountered unexpected error while subscribing to AgileConfig server: {e.__repr__()}. '
                    f'Will retry in 5 seconds.'
                )
            time.sleep(5)

    def stop(self):
        self._terminated = True
        if self._ws_client:
            self._ws_client.close()
            self._ws_client = None
        if self._running_thread:
            self._running_thread.join(timeout=5)
            self._running_thread = None
        self._ready = False
        self._config_cache = {}
        if self._updated_event.is_set():
            self._updated_event.clear()
        self._use_os_env_fallback = False

    def start(self):
        if self._running_thread:
            logger.info(f'The AgileConfig instance is already started.')
            return
        if not self._url or not self._app_id:
            logger.warning(
                "AgileConfig not fully configured. Missing AGILE_CONFIG_URL or AGILE_CONFIG_APP_ID. "
                "Will fall back to environment variables for all config lookups."
            )
            return

        self._running_thread = threading.Thread(target=self._start_config_listener, daemon=True)
        self._running_thread.start()

    def get_var_value(self, var_name: str, prefix =''):
        if not self._use_os_env_fallback:
            loaded = self._updated_event.wait(timeout=AGILE_CONFIG_TIMEOUT)
            if not loaded:
                self._use_os_env_fallback = True

        if self._use_os_env_fallback:
            logger.warning(
                'AgileConfig connection was not established. Falling back to using os.environ. '
                f'Prefix {prefix} will be ignored.'
            )
            return os.environ.get(var_name)
        return self._config_cache.get(f'{prefix}:{var_name}')

    def get_var(self, var_name: str, prefix =''):
        return ConfigStrWithUpdate(self, prefix=prefix, var_name=var_name)
