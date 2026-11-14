"""Client for the whoop-poc API (api.py) with an in-process fallback.

Client.request() sends the request to http://127.0.0.1:<port>. If nothing answers (connection refused,
timeout), it calls api.dispatch() in this process instead, which is the function the server runs, so the
caller gets the same document either way, and keeps doing that for RETRY_S before trying the server
again. An error status from a running server is not a reason to fall back: it raises ApiError like the
in-process path does. Client.mode says which path the last request took ("api" or "local").
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

import api

RETRY_S = 15               # after the server didn't answer, use the in-process path this long
TIMEOUT_S = 1.5            # polls (status, live, actions)
HEAVY_TIMEOUT_S = 120      # report / summary builds (NOOP's stager can take a while on a cold cache)


class Client:
    def __init__(self, cfg, port: int | None = None, pool=None, use_api: bool = True):
        """cfg: the args api.dispatch() needs locally (db, address, demo, heuristic, days). pool: a one-
        worker process pool for heavy local calls (None runs them in the calling thread). use_api False
        = always local (a --demo, --db, --address or --max-hr run, which the shared server doesn't
        serve)."""
        self.cfg, self.pool, self.use_api = cfg, pool, use_api
        self.port = port if port is not None else api.port()
        self.base = f"http://{api.HOST}:{self.port}"
        self.mode: str | None = None
        self._down_until = 0.0

    def get(self, path: str, **query) -> dict:
        return self.request("GET", path, {k: str(v) for k, v in query.items() if v is not None})

    def post(self, path: str, **body) -> dict:
        return self.request("POST", path, body=body)

    def request(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        query, body = query or {}, body or {}
        if self.use_api and time.monotonic() >= self._down_until:
            try:
                code, doc = self._http(method, path, query, body)
            except (urllib.error.URLError, OSError, ValueError):
                self._down_until = time.monotonic() + RETRY_S
            else:
                self.mode = "api"
                return _check(code, doc)
        self.mode = "local"
        return _check(*self._local(method, path, query, body))

    def _http(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        url = self.base + path + ("?" + urlencode(query) if query else "")
        data = json.dumps(body).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        timeout = HEAVY_TIMEOUT_S if api.is_heavy(method, path) else TIMEOUT_S
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:      # the server answered with an error status
            try:
                return e.code, json.loads(e.read())
            except ValueError:
                return e.code, {"error": f"HTTP {e.code}"}

    def _local(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        if self.pool is not None and api.is_heavy(method, path):
            try:
                code, doc = self.pool.submit(api.dispatch, self.cfg, method, path, query, body).result()
            except Exception:                    # noqa: BLE001 - worker died / pool broken: run it here
                self.pool = None
                code, doc = api.dispatch(self.cfg, method, path, query, body)
        else:
            code, doc = api.dispatch(self.cfg, method, path, query, body)
        return code, json.loads(json.dumps(doc))   # the same JSON types the HTTP path returns


def _check(code: int, doc: dict) -> dict:
    if 200 <= code < 300:
        return doc
    raise api.ApiError(code, doc.get("error", f"HTTP {code}") if isinstance(doc, dict) else f"HTTP {code}")
