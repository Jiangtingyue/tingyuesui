"""Shared HTTP client construction with safe local-proxy handling."""
from __future__ import annotations

from urllib.parse import urlsplit
from typing import Any

import httpx
from httpx._utils import get_environment_proxies


def make_async_http_client(**options: Any) -> httpx.AsyncClient:
    """Create an AsyncClient without letting a malformed proxy crash startup.

    Clash/Surge and similar desktop tools often export ``socks5h://``. httpx
    0.27 uses ``socks5://`` while still resolving hostnames through the proxy,
    so the alias is normalised here. Normal HTTP(S) proxies keep httpx's full
    environment and NO_PROXY behaviour.
    """
    environment = get_environment_proxies()
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
    changed = False
    for pattern, value in environment.items():
        proxy = value
        scheme = urlsplit(proxy).scheme.lower() if proxy else ""
        if scheme == "socks5h":
            proxy = "socks5://" + proxy.split("://", 1)[1]
            changed = True
        elif scheme and scheme not in {"http", "https", "socks5"}:
            print(f"[HTTP] 忽略不支持的代理协议: {scheme}")
            proxy = None
            changed = True
        try:
            mounts[pattern] = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
        except ImportError as exc:
            # A system proxy must never make the whole local companion fail at
            # import/startup. requirements.txt installs the SOCKS extra, but a
            # partially created environment still falls back to direct safely.
            print(f"[HTTP] SOCKS 组件不可用，已忽略该系统代理: {type(exc).__name__}")
            mounts[pattern] = None
            changed = True
    if changed:
        try:
            # 显式映射仍保留 NO_PROXY 生成的 pattern -> None 条目。
            return httpx.AsyncClient(mounts=mounts, trust_env=False, **options)
        except ImportError as exc:
            print(f"[HTTP] SOCKS 组件不可用，已忽略系统代理: {exc}")
            return httpx.AsyncClient(trust_env=False, **options)
    try:
        return httpx.AsyncClient(**options)
    except (ValueError, ImportError) as exc:
        print(f"[HTTP] 系统代理不可用，已切换为直连: {exc}")
        return httpx.AsyncClient(trust_env=False, **options)
