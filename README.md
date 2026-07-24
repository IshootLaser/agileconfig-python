# agileconfig-python

Python client for [AgileConfig](https://github.com/dotnetcore/AgileConfig). The client loads configuration over HTTP, listens for reload notifications over WebSocket, and falls back to `os.environ` when the server is unavailable.

## Installation

```bash
python -m pip install agileconfig-python
```

## Usage

```python
from agileconfig_python import AgileConfigLoader

loader = AgileConfigLoader(
	url="wss://config.example.com/ws",
	app_id="my-app",
	secret="your-secret",
	env="DEV",
)

database_host = str(loader.get_var("host", prefix="database"))
loader.stop()
```

The initial HTTP request populates the cache. When AgileConfig publishes a reload event, the cache is refreshed. If the server cannot be reached within the configured timeout, `get_var_value` reads the variable name from the process environment and ignores its prefix.

## API

- `AgileConfigLoader(url, app_id, secret, env)` creates the singleton loader and starts its listener.
- `loader.get_var_value(var_name, prefix="")` returns the current value, or an environment fallback.
- `loader.get_var(var_name, prefix="")` returns a lazy string-like proxy that reads the latest value when converted with `str()`.
- `loader.stop()` closes the WebSocket and stops the listener thread.

## Development

```bash
python -m pip install --upgrade pip build
python -m pip install -e .
python -m unittest discover -s unit_tests -v
python -m build
```

The live integration tests embedded in the source require an AgileConfig server and are not run by the offline unit-test command.

## Release checklist

1. Update `version` in `pyproject.toml`.
2. Run the unit tests and `python -m build`.
3. Inspect both artifacts with `python -m twine check dist/*`.
4. Upload to TestPyPI and install the uploaded version in a clean environment.
5. Create a Git tag matching the version, for example `v0.1.0`.
6. Publish to PyPI with a trusted publisher or `twine upload dist/*`.
