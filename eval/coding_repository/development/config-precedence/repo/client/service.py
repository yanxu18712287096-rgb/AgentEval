from .config import resolve
from .request import build_request

DEFAULTS = {"retries": 3, "cache": True, "label": "default"}


def prepare(url, file_config=None, env=None, cli=None):
    config = resolve(DEFAULTS, file_config or {}, env or {}, cli or {})
    return build_request(config, url)
