"""Client HTTP de base : session réutilisable, retry, rate-limit doux."""

from __future__ import annotations

import time
from typing import Any, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..logger import get_logger

log = get_logger("http")


class HttpError(Exception):
    pass


class RateLimitError(HttpError):
    pass


class HttpClient:
    """Wrapper requests avec retry exponentiel et back-off sur 429."""

    def __init__(self, base_url: str = "", default_headers: Optional[dict] = None,
                 timeout: int = 15, min_interval_sec: float = 0.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval_sec = min_interval_sec
        self._last_call = 0.0
        self.session = requests.Session()
        if default_headers:
            self.session.headers.update(default_headers)
        self.session.headers.setdefault(
            "User-Agent", "solana-smallcap-bot/1.0 (+research)"
        )

    def _throttle(self) -> None:
        if self.min_interval_sec <= 0:
            return
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval_sec:
            time.sleep(self.min_interval_sec - elapsed)
        self._last_call = time.time()

    @retry(
        retry=retry_if_exception_type((requests.RequestException, RateLimitError)),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._throttle()
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.request(method, url, **kwargs)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            log.warning("Rate limit (429) sur %s — attente %.1fs", url, retry_after)
            time.sleep(retry_after)
            raise RateLimitError(url)

        if resp.status_code >= 500:
            raise HttpError(f"{resp.status_code} serveur sur {url}")

        if resp.status_code >= 400:
            # 4xx (hors 429) = pas de retry, on remonte un None propre
            log.debug("HTTP %s sur %s : %s", resp.status_code, url, resp.text[:200])
            raise HttpError(f"{resp.status_code} sur {url}")

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)
