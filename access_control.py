"""Local-first access guard with Host, session and request-origin checks."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request

import config


class AccessControl:
    cookie_name = "daxigua_access"
    header_name = "x-daxigua-access-token"

    def __init__(self) -> None:
        self._token: str | None = None
        self._attempts: dict[str, list[float]] = {}

    @property
    def token_path(self) -> Path:
        configured = str(os.getenv("DAXIGUA_ACCESS_TOKEN_FILE", "")).strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return Path(config.DB_PATH).resolve().parent / "access-token.txt"

    def prepare(self) -> str:
        """Load or create a pairing token without placing it in frontend code."""
        if self._token:
            return self._token
        configured = str(os.getenv("DAXIGUA_ACCESS_TOKEN", "")).strip()
        if configured:
            self._token = configured
            return configured
        path = self.token_path
        try:
            existing = path.read_text("utf-8").strip()
        except FileNotFoundError:
            existing = ""
        if existing:
            self._token = existing
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(24)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self._token = token
        return token

    @staticmethod
    def _is_loopback(host: str) -> bool:
        value = str(host or "").strip().split("%", 1)[0]
        if value in {"localhost", "testclient", "testserver"}:
            return True
        try:
            return ipaddress.ip_address(value).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _host_without_port(host_header: str) -> str:
        raw = str(host_header or "").strip()
        if not raw:
            return ""
        try:
            parsed = urlsplit(f"//{raw}")
            return (parsed.hostname or "").rstrip(".").lower()
        except ValueError:
            return ""

    @staticmethod
    def _port_from_host(host_header: str, scheme: str) -> int:
        try:
            parsed = urlsplit(f"//{host_header}")
            if parsed.port is not None:
                return parsed.port
        except ValueError:
            return -1
        return 443 if scheme == "https" else 80

    def allowed_host(self, host_header: str) -> bool:
        host = self._host_without_port(host_header)
        if not host:
            return False
        configured = {
            item.strip().rstrip(".").lower()
            for item in os.getenv("DAXIGUA_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if host in {"localhost", "testserver", "testclient"} or host in configured:
            return True
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return False
        # Numeric loopback/private LAN hosts are safe defaults. Public VPS IPs
        # and domain names must be explicitly listed in DAXIGUA_ALLOWED_HOSTS.
        return address.is_loopback or address.is_private

    def client_host(self, request: Request) -> str:
        immediate = str(request.client.host if request.client else "")
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        # Only trust a forwarding header received from a local reverse proxy.
        if forwarded and self._is_loopback(immediate):
            return forwarded
        return immediate

    def effective_scheme(self, request: Request) -> str:
        """Trust proxy scheme headers only from a loopback reverse proxy."""
        immediate = str(request.client.host if request.client else "")
        if self._is_loopback(immediate):
            forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
            if forwarded in {"http", "https"}:
                return forwarded
        return str(request.url.scheme or "http").lower()

    def remote_required(self, request: Request) -> bool:
        if str(os.getenv("DAXIGUA_PROTECT_REMOTE", "true")).strip().lower() in {
            "0", "false", "no", "off",
        }:
            return False
        return not self._is_loopback(self.client_host(request))

    def _cookie_value(self) -> str:
        token = self.prepare().encode("utf-8")
        return hmac.new(token, b"daxigua-browser-session-v2", hashlib.sha256).hexdigest()

    def has_header_token(self, request: Request) -> bool:
        supplied = str(request.headers.get(self.header_name, ""))
        return bool(supplied and hmac.compare_digest(supplied, self.prepare()))

    def has_session_cookie(self, request: Request) -> bool:
        supplied = str(request.cookies.get(self.cookie_name, ""))
        return bool(supplied and hmac.compare_digest(supplied, self._cookie_value()))

    def authenticated(self, request: Request) -> bool:
        # Loopback is no longer treated as a browser identity. Loading the root
        # page creates a local HttpOnly session cookie without user friction;
        # scripts/API clients may instead use the pairing-token header.
        return self.has_header_token(request) or self.has_session_cookie(request)

    def origin_allowed(self, request: Request) -> bool:
        origin = str(request.headers.get("origin", "")).strip()
        if not origin:
            return True
        if origin == "null":
            return False
        configured = {
            item.strip().rstrip("/")
            for item in os.getenv("DAXIGUA_CORS_ORIGINS", "").split(",")
            if item.strip() and item.strip() != "*"
        }
        if origin.rstrip("/") in configured:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        request_host = self._host_without_port(request.headers.get("host", ""))
        if parsed.hostname.rstrip(".").lower() != request_host:
            return False
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_scheme = self.effective_scheme(request)
        return origin_port == self._port_from_host(
            request.headers.get("host", ""), request_scheme
        )

    def status(self, request: Request) -> dict[str, Any]:
        required = self.remote_required(request)
        return {
            "required": required,
            "authenticated": self.authenticated(request),
            "local_request": not required,
            "trusted_host": self.allowed_host(request.headers.get("host", "")),
            "https": self.effective_scheme(request) == "https",
        }

    def allow_attempt(self, request: Request) -> bool:
        now = time.monotonic()
        host = self.client_host(request) or "unknown"
        recent = [stamp for stamp in self._attempts.get(host, []) if now - stamp < 300]
        if len(recent) >= 8:
            self._attempts[host] = recent
            return False
        recent.append(now)
        self._attempts[host] = recent
        return True

    def verify_token(self, supplied: str) -> bool:
        return hmac.compare_digest(str(supplied or "").strip(), self.prepare())

    def clear_attempts(self, request: Request) -> None:
        self._attempts.pop(self.client_host(request) or "unknown", None)

    def cookie_value(self) -> str:
        return self._cookie_value()


access_control = AccessControl()
