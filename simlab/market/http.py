"""HTTP 工具：多 host 回退。"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

from simlab import config

logger = logging.getLogger("simlab.http")
_SSL = ssl.create_default_context()


def fetch_json(path: str, hosts: list[str], *, timeout: float | None = None) -> Any | None:
    timeout = timeout if timeout is not None else config.HTTP_TIMEOUT
    last_err: Exception | None = None
    for host in hosts:
        url = f"{host.rstrip('/')}{path}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            logger.debug("HTTP fail %s: %s", url, exc)
            continue
    if last_err:
        logger.warning("All hosts failed for %s: %s", path, last_err)
    return None
