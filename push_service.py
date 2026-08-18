"""Web Push service with bounded subscriptions and SSRF-safe endpoints."""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from urllib.parse import urlsplit

try:
    from pywebpush import WebPushException, webpush
except Exception:  # Optional enhancement: web chat must still start without it.
    class WebPushException(Exception):
        response = None

    def webpush(*args, **kwargs):
        raise RuntimeError("pywebpush 未安装，网页内主动消息仍可用，但系统通知不可用")

from config import PUSH_CONFIG
from models import get_db


MAX_SUBSCRIPTIONS = 32
MAX_ENDPOINT_CHARS = 2048
MAX_KEY_CHARS = 512


def _validate_endpoint(endpoint: str) -> str:
    endpoint = str(endpoint or "").strip()
    if not endpoint or len(endpoint) > MAX_ENDPOINT_CHARS:
        raise ValueError("推送 endpoint 为空或过长")
    try:
        parsed = urlsplit(endpoint)
    except ValueError as exc:
        raise ValueError("推送 endpoint 格式无效") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("推送 endpoint 必须是 HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("推送 endpoint 包含不允许的凭据或片段")
    host = parsed.hostname.rstrip(".")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal and (
        literal.is_private or literal.is_loopback or literal.is_link_local
        or literal.is_reserved or literal.is_multicast or literal.is_unspecified
    ):
        raise ValueError("推送 endpoint 不能指向本机或私有网络")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise ValueError("推送 endpoint 域名无法解析") from exc
    if not addresses:
        raise ValueError("推送 endpoint 域名没有可用地址")
    for value in addresses:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified
        ):
            raise ValueError("推送 endpoint 解析到了本机或私有网络")
    return endpoint


class PushService:
    """PWA Web Push 推送。"""

    def __init__(self):
        self.vapid_private = PUSH_CONFIG["vapid_private_key"]
        self.vapid_public = PUSH_CONFIG["vapid_public_key"]
        self.vapid_claims = {"sub": PUSH_CONFIG["vapid_claims_email"]}
        self._send_lock = threading.Lock()
        self._last_send_at = 0.0

    def subscribe(self, subscription_info: dict) -> bool:
        if not isinstance(subscription_info, dict):
            return False
        try:
            endpoint = _validate_endpoint(subscription_info.get("endpoint", ""))
        except ValueError:
            return False
        keys = subscription_info.get("keys", {})
        if not isinstance(keys, dict):
            return False
        p256dh = str(keys.get("p256dh") or "")
        auth = str(keys.get("auth") or "")
        if (
            not p256dh or not auth
            or len(p256dh) > MAX_KEY_CHARS or len(auth) > MAX_KEY_CHARS
        ):
            return False

        with get_db() as db:
            current = db.execute(
                "SELECT COUNT(*) FROM push_subscriptions WHERE active = 1"
            ).fetchone()[0]
            exists = db.execute(
                "SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
            ).fetchone()
            if current >= MAX_SUBSCRIPTIONS and not exists:
                return False
            db.execute(
                """INSERT OR REPLACE INTO push_subscriptions
                (endpoint, keys_p256dh, keys_auth, active)
                VALUES (?, ?, ?, 1)""",
                (endpoint, p256dh, auth),
            )
        return True

    def unsubscribe(self, endpoint: str):
        endpoint = str(endpoint or "")[:MAX_ENDPOINT_CHARS]
        with get_db() as db:
            db.execute(
                "UPDATE push_subscriptions SET active = 0 WHERE endpoint = ?",
                (endpoint,),
            )

    def send_notification(
        self, title: str, body: str, data: dict | None = None, tag: str = "default"
    ) -> int:
        if not self.vapid_private or not self.vapid_public:
            return 0
        with self._send_lock:
            now = time.monotonic()
            if now - self._last_send_at < 2.0:
                return 0
            self._last_send_at = now

        with get_db() as db:
            subs = db.execute(
                "SELECT * FROM push_subscriptions WHERE active = 1 LIMIT ?",
                (MAX_SUBSCRIPTIONS,),
            ).fetchall()

        payload = json.dumps({
            "title": str(title or "")[:120],
            "body": str(body or "")[:500],
            "data": data if isinstance(data, dict) else {},
            "tag": str(tag or "default")[:80],
            "icon": "/static/icons/icon-192.png",
            "badge": "/static/icons/badge-72.png",
        })

        sent = 0
        for sub in subs:
            try:
                endpoint = _validate_endpoint(sub["endpoint"])
            except ValueError:
                with get_db() as db:
                    db.execute(
                        "UPDATE push_subscriptions SET active = 0 WHERE endpoint = ?",
                        (sub["endpoint"],),
                    )
                continue
            sub_info = {
                "endpoint": endpoint,
                "keys": {"p256dh": sub["keys_p256dh"], "auth": sub["keys_auth"]},
            }
            try:
                webpush(
                    subscription_info=sub_info,
                    data=payload,
                    vapid_private_key=self.vapid_private,
                    vapid_claims=self.vapid_claims,
                    timeout=10,
                )
                sent += 1
            except WebPushException as exc:
                if exc.response and exc.response.status_code in (404, 410):
                    with get_db() as db:
                        db.execute(
                            "UPDATE push_subscriptions SET active = 0 WHERE endpoint = ?",
                            (endpoint,),
                        )
            except Exception:
                # Detailed upstream errors stay in server diagnostics, not API output.
                continue
        return sent

    def get_vapid_public_key(self) -> str:
        return self.vapid_public

    def status(self) -> dict:
        with get_db() as db:
            active = int(db.execute(
                "SELECT COUNT(*) FROM push_subscriptions WHERE active=1"
            ).fetchone()[0] or 0)
        return {
            "configured": bool(self.vapid_private and self.vapid_public),
            "active_subscriptions": active,
            "ready": bool(self.vapid_private and self.vapid_public and active),
        }


def generate_vapid_keys():
    from py_vapid import Vapid
    vapid = Vapid()
    vapid.generate_keys()
    print("VAPID_PRIVATE_KEY:", vapid.private_pem())
    print("VAPID_PUBLIC_KEY:", vapid.public_key)
    return {"private": vapid.private_pem(), "public": vapid.public_key}


push_service = PushService()
