"""Thin client for the OpenAI Ads (ChatGPT Ads) Advertiser API.

Standard library only. Docs: https://developers.openai.com/ads/llms.txt
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://api.ads.openai.com/v1"


class AdsError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:2000]}")
        self.status = status
        self.body = body


def _load_env_file() -> None:
    """Read KEY=VALUE lines from .env beside this file. Real env vars win."""
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class AdsClient:
    def __init__(self) -> None:
        _load_env_file()
        self.base_url = os.environ.get("OPENAI_ADS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self._key = os.environ.get("OPENAI_ADS_API_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _encode(self, params: dict | None) -> str:
        if not params:
            return ""
        pairs: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            for item in value if isinstance(value, (list, tuple)) else [value]:
                if isinstance(item, (dict, list)):
                    item = json.dumps(item, separators=(",", ":"))
                elif isinstance(item, bool):
                    item = "true" if item else "false"
                pairs.append((key, str(item)))
        return "?" + urllib.parse.urlencode(pairs) if pairs else ""

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None, idempotency_key: str | None = None,
                retries: int = 3):
        if not self._key:
            raise AdsError(0, "OPENAI_ADS_API_KEY is not set (env var or .env beside client.py)")
        if not path.startswith("/") or ".." in path or "://" in path:
            raise AdsError(0, f"Refusing path {path!r}")
        url = self.base_url + path + self._encode(params)
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json",
                   "User-Agent": "local-openai-ads-mcp/0.1"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    text = resp.read().decode("utf-8", "replace")
                    return json.loads(text) if text.strip() else {}
            except urllib.error.HTTPError as err:
                text = err.read().decode("utf-8", "replace")
                # Retry throttling and server errors. Writes are only retried
                # when they carry an idempotency key.
                retryable = err.code == 429 or err.code >= 500
                safe = method == "GET" or bool(idempotency_key)
                if retryable and safe and attempt <= retries:
                    wait = err.headers.get("Retry-After")
                    time.sleep(min(float(wait) if wait and wait.isdigit() else 2 ** attempt, 30))
                    continue
                raise AdsError(err.code, text) from None
            except urllib.error.URLError as err:
                if method == "GET" and attempt <= retries:
                    time.sleep(2 ** attempt)
                    continue
                raise AdsError(0, f"Network error: {err.reason}") from None

    def get(self, path: str, params: dict | None = None):
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict | None = None, idempotency_key: str | None = None):
        return self.request("POST", path, body=body if body is not None else {},
                            idempotency_key=idempotency_key)
