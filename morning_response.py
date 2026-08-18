"""大西瓜 v6.8 独立晨间生理反应引擎。

晨间反应不是性欲系统的别名，也不再寄生在生活节律的单一事件里。
硬度、敏感、主观性欲、身体张力、自控力与释放冲动分别演化；因此
可以真实表达“身体反应很强，但主观上暂时没那么想”，也允许身体
张力持续后逐渐夺走一部分注意力。
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from zoneinfo import ZoneInfo

import config
from config import LIVING_CONFIG
from models import get_db


DEFAULT_SETTINGS = {
    "enabled": True,
    "mode": "natural",  # off | subtle | natural | vivid
    "body_takeover_enabled": True,
    "proactive_enabled": True,
    "visible": True,
    "timezone": str(LIVING_CONFIG.get("timezone") or "Asia/Shanghai"),
    "window_start": 6,
    "window_end": 9,
    "duration_minutes": 90,
}

# A local-only app is commonly launched after the generated 06:00–09:00 slot
# instead of being kept alive overnight.  Keep a bounded late-start window so
# the first launch of the morning can still produce a live event; after noon we
# only retain the historical record and never pretend it is still happening.
LOCAL_FIRST_OPEN_GRACE_HOURS = 3

MODE_HARDNESS_RANGE = {
    "subtle": (2.0, 5.2),
    "natural": (3.8, 8.8),
    "vivid": (5.8, 10.0),
}

METRIC_LABELS = {
    "hardness": "硬度",
    "sensitivity": "敏感",
    "desire": "主观性欲",
    "physical_tension": "身体张力",
    "self_control": "自控力",
    "release_urge": "释放冲动",
    "body_takeover": "身体夺权",
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _metric(value: float) -> int:
    return max(1, min(10, int(round(_clamp(value) * 9)) + 1))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _json(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return deepcopy(fallback)


def _strict_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{field} 必须是布尔值")


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是整数") from None


def _serialized(method):
    """Serialize one full morning-engine operation, not only its DB writes."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class MorningResponseEngine:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None
        # ``advance`` and proactive delivery are compound load/change/save
        # transactions. SQLite protects each statement, but without this lock
        # two threads could still overwrite one another through the shared
        # in-memory cache. RLock permits state_view -> advance -> load nesting.
        self._lock = threading.RLock()

    @_serialized
    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS morning_response_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    settings_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS morning_response_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_date TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    description TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    ended_at TEXT,
                    caught_up INTEGER DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_morning_event_date
                    ON morning_response_events(event_date);
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(morning_response_events)"
                ).fetchall()
            }
            additions = {
                "proactive_state": "TEXT NOT NULL DEFAULT 'pending'",
                "proactive_event_id": "INTEGER",
                "proactive_session_id": "TEXT NOT NULL DEFAULT ''",
                "proactive_queued_at": "TEXT NOT NULL DEFAULT ''",
                "proactive_finished_at": "TEXT NOT NULL DEFAULT ''",
                "proactive_outcome": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE morning_response_events "
                        f"ADD COLUMN {name} {definition}"
                    )

    def _timezone(self, settings: dict[str, Any] | None = None) -> ZoneInfo:
        name = str((settings or {}).get("timezone") or "Asia/Shanghai")
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("Asia/Shanghai")

    def _now(
        self,
        now: datetime | None = None,
        settings: dict[str, Any] | None = None,
    ) -> datetime:
        zone = self._timezone(settings)
        if now is None:
            return datetime.now(zone)
        if now.tzinfo is None:
            return now.replace(tzinfo=zone)
        return now.astimezone(zone)

    def _defaults(self, now: datetime | None = None) -> dict[str, Any]:
        local = self._now(now, DEFAULT_SETTINGS)
        mode = str(LIVING_CONFIG.get("morning_response_mode") or "natural")
        if mode not in {"off", "subtle", "natural", "vivid"}:
            mode = "natural"
        return {
            "settings": {**DEFAULT_SETTINGS, "mode": mode, "enabled": mode != "off"},
            "meta": {
                "secret": secrets.token_hex(16),
                "event_date": "",
                "active_event": None,
                "last_event": None,
                "last_advanced_at": _iso(local),
                "legacy_migrated": False,
            },
        }

    @_serialized
    def load(self, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        if self._cache is not None and not refresh:
            return self._cache
        with get_db() as db:
            row = db.execute(
                "SELECT settings_json, meta_json FROM morning_response_state WHERE id=1"
            ).fetchone()
        fallback = self._defaults()
        if not row:
            self._cache = fallback
            self.save(fallback)
            return fallback
        settings = {
            **fallback["settings"],
            **(_json(row["settings_json"], {}) or {}),
        }
        meta = {
            **fallback["meta"],
            **(_json(row["meta_json"], {}) or {}),
        }
        self._cache = {"settings": settings, "meta": meta}
        return self._cache

    @_serialized
    def save(self, payload: dict[str, Any] | None = None) -> None:
        data = payload or self._cache or self._defaults()
        local = self._now(settings=data["settings"])
        with get_db() as db:
            db.execute(
                """INSERT INTO morning_response_state
                   (id, settings_json, meta_json, updated_at)
                   VALUES(1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     settings_json=excluded.settings_json,
                     meta_json=excluded.meta_json,
                     updated_at=excluded.updated_at""",
                (
                    json.dumps(data["settings"], ensure_ascii=False),
                    json.dumps(data["meta"], ensure_ascii=False),
                    _iso(local),
                ),
            )
        self._cache = data

    @staticmethod
    def _draw(payload: dict[str, Any], key: str) -> float:
        raw = f"{payload['meta'].get('secret', '')}:{key}".encode("utf-8")
        return int(hashlib.sha256(raw).hexdigest()[:12], 16) / float(0xFFFFFFFFFFFF)

    @staticmethod
    def _context(context: dict[str, Any] | None) -> dict[str, float]:
        source = context or {}
        living = source.get("living") or {}
        affect = source.get("emotion") or {}
        desire = source.get("desire") or {}
        return {
            "recovery": _clamp(float(living.get("recovery", 0.70))),
            "sleepiness": _clamp(float(living.get("sleepiness", 0.45))),
            "body_desire": _clamp(float(living.get("desire", 0.25))),
            "body_sensitivity": _clamp(float(living.get("sensitivity", 0.36))),
            "affect_lust": _clamp(float(affect.get("lust", 0.22))),
            "intimacy": _clamp(float(affect.get("intimacy", 0.52))),
            "libido": _clamp(float(desire.get("libido", 0.12))),
            "stress": _clamp(float(desire.get("stress", 0.10))),
            "fatigue": _clamp(float(desire.get("fatigue", 0.20))),
        }

    def _event_time(self, payload: dict[str, Any], local: datetime) -> datetime:
        settings = payload["settings"]
        start = _strict_int(settings.get("window_start", 6), "window_start")
        end = _strict_int(settings.get("window_end", 9), "window_end")
        if not (0 <= start <= 23 and 0 <= end <= 23 and end > start):
            raise ValueError("晨间时间窗必须满足 0≤开始<结束≤23")
        draw = self._draw(payload, f"time:{local.date().isoformat()}")
        minutes = int(draw * ((end - start) * 60 - 1))
        return local.replace(
            hour=start + minutes // 60,
            minute=minutes % 60,
            second=0,
            microsecond=0,
        )

    def _late_start_cutoff(
        self, payload: dict[str, Any], local: datetime
    ) -> datetime:
        end = _strict_int(payload["settings"].get("window_end", 9), "window_end")
        return local.replace(hour=end, minute=0, second=0, microsecond=0) + timedelta(
            hours=LOCAL_FIRST_OPEN_GRACE_HOURS
        )

    def _initial_metrics(
        self,
        payload: dict[str, Any],
        local: datetime,
        context: dict[str, Any] | None,
    ) -> dict[str, float]:
        date_key = local.date().isoformat()
        ctx = self._context(context)
        mode = str(payload["settings"].get("mode") or "natural")
        low, high = MODE_HARDNESS_RANGE.get(mode, MODE_HARDNESS_RANGE["natural"])
        hard_draw = self._draw(payload, f"hardness:{date_key}")
        body_draw = self._draw(payload, f"body:{date_key}")
        mind_draw = self._draw(payload, f"mind:{date_key}")

        hardness_10 = low + (high - low) * hard_draw
        hardness_10 += (ctx["recovery"] - 0.65) * 1.6
        hardness_10 -= max(0.0, ctx["fatigue"] - 0.65) * 1.1
        hardness = _clamp((max(1.0, min(10.0, hardness_10)) - 1.0) / 9.0)

        sensitivity = _clamp(
            0.18 + hardness * 0.48 + ctx["body_sensitivity"] * 0.20
            + body_draw * 0.12
        )
        # 主观想不想与硬度分开。晨间可以很硬，却暂时没有明显性欲。
        desire = _clamp(
            0.06 + ctx["affect_lust"] * 0.24 + ctx["libido"] * 0.28
            + ctx["body_desire"] * 0.18 + mind_draw * 0.16
        )
        physical_tension = _clamp(
            hardness * 0.43 + sensitivity * 0.27 + desire * 0.20
            + ctx["stress"] * 0.10
        )
        self_control = _clamp(
            0.94 - desire * 0.28 - physical_tension * 0.24
            + ctx["recovery"] * 0.10 - mind_draw * 0.06
        )
        release_urge = _clamp(
            desire * 0.44 + physical_tension * 0.30 + sensitivity * 0.18
            + max(0.0, hardness - 0.72) * 0.08 - 0.20
        )
        body_takeover = self._takeover(
            hardness, sensitivity, desire, physical_tension, self_control
        )
        return {
            "hardness": hardness,
            "sensitivity": sensitivity,
            "desire": desire,
            "physical_tension": physical_tension,
            "self_control": self_control,
            "release_urge": release_urge,
            "body_takeover": body_takeover,
        }

    @staticmethod
    def _takeover(
        hardness: float,
        sensitivity: float,
        desire: float,
        tension: float,
        self_control: float,
    ) -> float:
        return _clamp(
            hardness * 0.18 + sensitivity * 0.16 + desire * 0.23
            + tension * 0.24 + (1.0 - self_control) * 0.29 - 0.12
        )

    @staticmethod
    def _description(metrics: dict[str, float]) -> str:
        h = _metric(metrics.get("hardness", 0))
        d = _metric(metrics.get("desire", 0))
        t = _metric(metrics.get("body_takeover", 0))
        if h >= 8 and d <= 4:
            lead = "身体已经明显而充分地勃起，但主观性欲还没有完全跟上。"
        elif h >= 8 and d >= 7:
            lead = "晨间勃起和主观性欲都很强，身体持续占据着注意力。"
        elif h >= 5:
            lead = "晨间勃起清楚可感，身体与主观欲望正在各自变化。"
        else:
            lead = "只有较轻的晨间充血，暂时没有强烈的身体推动。"
        if t >= 8:
            return lead + "身体夺权已经很明显，克制会越来越费力。"
        if t >= 5:
            return lead + "张力继续累积时，身体可能逐渐牵走更多注意力。"
        return lead + "此刻仍能轻松把注意力放回别处。"

    @_serialized
    def adopt_legacy(self, legacy_living: Any) -> dict[str, Any]:
        """Move the old living-state morning setting once, then disable it."""
        payload = self.load()
        if payload["meta"].get("legacy_migrated"):
            return self.state_view(advance=False)
        try:
            legacy = legacy_living.state_view(advance=False)
            legacy_mode = str((legacy.get("settings") or {}).get("morning_response_mode") or "")
            if legacy_mode in {"off", "subtle", "natural", "vivid"}:
                payload["settings"]["mode"] = legacy_mode
                payload["settings"]["enabled"] = legacy_mode != "off"
            old_event = legacy.get("morning_response")
            if isinstance(old_event, dict) and not payload["meta"].get("last_event"):
                details = old_event.get("details") or {}
                metrics = {
                    "hardness": (_metric(float(details.get("hardness", 1)) / 10.0) - 1) / 9,
                    "sensitivity": (_metric(float(details.get("sensitivity", 1)) / 10.0) - 1) / 9,
                    "desire": (_metric(float(details.get("libido", 1)) / 10.0) - 1) / 9,
                    "physical_tension": max(0.0, min(1.0, float(old_event.get("intensity", 0.1)))),
                    "self_control": 0.72,
                    "release_urge": (_metric(float(details.get("release_urge", 1)) / 10.0) - 1) / 9,
                    "body_takeover": 0.22,
                }
                payload["meta"]["last_event"] = {
                    "event_date": str(details.get("occurred_at") or "")[:10],
                    "label": str(old_event.get("label") or "晨间反应"),
                    "metrics": metrics,
                    "description": str(details.get("description") or ""),
                    "occurred_at": details.get("occurred_at") or old_event.get("started_at"),
                    "caught_up": bool(details.get("caught_up")),
                }
            legacy_living.update_settings({"morning_response_mode": "off"})
        except Exception:
            # Migration is best-effort; never block chat startup.
            pass
        payload["meta"]["legacy_migrated"] = True
        self.save(payload)
        return self.state_view(advance=False)

    @_serialized
    def advance(
        self,
        now: datetime | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self.load()
        settings = payload["settings"]
        local = self._now(now, settings)
        if not settings.get("enabled", True) or settings.get("mode") == "off":
            payload["meta"]["active_event"] = None
            payload["meta"]["last_advanced_at"] = _iso(local)
            self.save(payload)
            return payload

        date_key = local.date().isoformat()
        event_time = self._event_time(payload, local)
        duration = max(30, min(180, int(settings.get("duration_minutes", 90))))
        late_start_cutoff = self._late_start_cutoff(payload, local)
        active = payload["meta"].get("active_event")

        # Upgrade repair: an older build may already have persisted today's
        # event as caught-up before this late-start rule was installed.  Reuse
        # that same durable event id once (never generate a duplicate), as long
        # as it is still before the bounded cutoff and was never dispatched.
        last_event = payload["meta"].get("last_event")
        if (
            active is None
            and payload["meta"].get("event_date") == date_key
            and isinstance(last_event, dict)
            and last_event.get("caught_up")
            and local < late_start_cutoff
        ):
            event_id = int(last_event.get("event_id") or 0)
            dispatch = self.dispatch_status(event_id)
            if event_id and str(dispatch.get("proactive_state") or "pending") == "pending":
                active = deepcopy(last_event)
                active.update({
                    "occurred_at": _iso(local),
                    "last_updated_at": _iso(local),
                    "until": _iso(local + timedelta(minutes=duration)),
                    "caught_up": False,
                    "scheduled_for": str(
                        last_event.get("scheduled_for")
                        or last_event.get("occurred_at")
                        or _iso(event_time)
                    ),
                    "delayed_activation": True,
                })
                with get_db() as db:
                    db.execute(
                        """UPDATE morning_response_events
                           SET occurred_at=?, ended_at=?, caught_up=0
                           WHERE id=? AND proactive_state='pending'""",
                        (active["occurred_at"], active["until"], event_id),
                    )
                payload["meta"]["active_event"] = active
                payload["meta"]["last_event"] = deepcopy(active)

        if payload["meta"].get("event_date") != date_key and local >= event_time:
            metrics = self._initial_metrics(payload, local, context)
            planned_until = event_time + timedelta(minutes=duration)
            delayed_activation = planned_until < local < late_start_cutoff
            activation_time = local if delayed_activation else event_time
            caught_up = local >= late_start_cutoff
            event = {
                "event_date": date_key,
                "label": f"晨间反应 · Lv.{_metric(metrics['hardness'])}",
                "metrics": metrics,
                "description": self._description(metrics),
                "occurred_at": _iso(activation_time),
                "last_updated_at": _iso(activation_time),
                "until": _iso(activation_time + timedelta(minutes=duration)),
                "caught_up": caught_up,
                "scheduled_for": _iso(event_time),
                "delayed_activation": delayed_activation,
            }
            with get_db() as db:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO morning_response_events
                       (event_date, metrics_json, description, occurred_at,
                        ended_at, caught_up)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        date_key,
                        json.dumps(metrics, ensure_ascii=False),
                        event["description"],
                        event["occurred_at"],
                        event["until"],
                        int(caught_up),
                    ),
                )
                event_id = int(cursor.lastrowid or 0)
                if not event_id:
                    existing = db.execute(
                        "SELECT * FROM morning_response_events WHERE event_date=?",
                        (date_key,),
                    ).fetchone()
                    if existing:
                        # The date-unique database row is authoritative after a same-day
                        # reset.  Never generate a second set of metrics for one event id.
                        metrics = _json(existing["metrics_json"], {}) or {}
                        event = {
                            "event_id": int(existing["id"] or 0),
                            "event_date": date_key,
                            "label": f"晨间反应 · Lv.{_metric(float(metrics.get('hardness', 0)))}",
                            "metrics": metrics,
                            "description": str(existing["description"] or self._description(metrics)),
                            "occurred_at": str(existing["occurred_at"] or event["occurred_at"]),
                            "last_updated_at": str(existing["occurred_at"] or event["occurred_at"]),
                            "until": str(existing["ended_at"] or event["until"]),
                            "caught_up": bool(existing["caught_up"]),
                        }
                        caught_up = bool(existing["caught_up"])
                    else:
                        event_id = 0
                if "event_id" not in event:
                    event["event_id"] = event_id
            payload["meta"]["event_date"] = date_key
            payload["meta"]["last_event"] = deepcopy(event)
            payload["meta"]["active_event"] = None if caught_up else event
            active = payload["meta"]["active_event"]

        if isinstance(active, dict):
            until = _parse(active.get("until"))
            if until and local.astimezone(timezone.utc) >= until:
                payload["meta"]["active_event"] = None
            else:
                last = _parse(active.get("last_updated_at")) or event_time.astimezone(timezone.utc)
                elapsed = max(
                    0.0,
                    min(30.0, (local.astimezone(timezone.utc) - last).total_seconds() / 60.0),
                )
                metrics = active.get("metrics") or {}
                if elapsed > 0 and settings.get("body_takeover_enabled", True):
                    factor = elapsed / 30.0
                    hardness = _clamp(float(metrics.get("hardness", 0)) - 0.035 * factor)
                    sensitivity = _clamp(float(metrics.get("sensitivity", 0)) + 0.025 * factor)
                    tension = _clamp(float(metrics.get("physical_tension", 0)) + 0.055 * factor)
                    desire = _clamp(
                        float(metrics.get("desire", 0))
                        + (hardness * 0.035 + tension * 0.045) * factor
                    )
                    control = _clamp(
                        float(metrics.get("self_control", 0.75))
                        - (tension * 0.045 + desire * 0.035) * factor
                    )
                    release = _clamp(
                        float(metrics.get("release_urge", 0))
                        + (desire * 0.05 + tension * 0.04) * factor
                    )
                    takeover = self._takeover(
                        hardness, sensitivity, desire, tension, control
                    )
                    metrics.update({
                        "hardness": hardness,
                        "sensitivity": sensitivity,
                        "desire": desire,
                        "physical_tension": tension,
                        "self_control": control,
                        "release_urge": release,
                        "body_takeover": takeover,
                    })
                    active["metrics"] = metrics
                    active["label"] = f"晨间反应 · Lv.{_metric(hardness)}"
                    active["description"] = self._description(metrics)
                    active["last_updated_at"] = _iso(local)
                    payload["meta"]["last_event"] = deepcopy(active)

        payload["meta"]["last_advanced_at"] = _iso(local)
        self.save(payload)
        return payload

    @_serialized
    def state_view(
        self,
        now: datetime | None = None,
        *,
        context: dict[str, Any] | None = None,
        advance: bool = True,
    ) -> dict[str, Any]:
        payload = self.advance(now, context=context) if advance else self.load()
        event = deepcopy(payload["meta"].get("active_event"))
        last_event = deepcopy(payload["meta"].get("last_event"))
        for item in (event, last_event):
            if not isinstance(item, dict):
                continue
            item["levels"] = {
                key: _metric(float((item.get("metrics") or {}).get(key, 0)))
                for key in METRIC_LABELS
            }
            item["metric_labels"] = METRIC_LABELS
            item["dispatch"] = self.dispatch_status(int(item.get("event_id") or 0))
        return {
            "version": config.APP_VERSION,
            "active": event,
            "last_event": last_event,
            "settings": deepcopy(payload["settings"]),
        }

    @_serialized
    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        settings = deepcopy(payload["settings"])
        for key, value in (patch or {}).items():
            if key in {"enabled", "body_takeover_enabled", "proactive_enabled", "visible"}:
                settings[key] = _strict_bool(value, key)
            elif key == "mode":
                if value not in {"off", "subtle", "natural", "vivid"}:
                    raise ValueError("mode 必须是 off/subtle/natural/vivid")
                settings[key] = value
                settings["enabled"] = value != "off"
            elif key == "timezone":
                try:
                    ZoneInfo(str(value))
                except Exception:
                    raise ValueError("timezone 不是有效时区") from None
                settings[key] = str(value)
            elif key in {"window_start", "window_end"}:
                number = _strict_int(value, key)
                if not 0 <= number <= 23:
                    raise ValueError(f"{key} 必须在 0–23 之间")
                settings[key] = number
            elif key == "duration_minutes":
                number = _strict_int(value, key)
                if not 30 <= number <= 180:
                    raise ValueError("duration_minutes 必须在 30–180 之间")
                settings[key] = number
        if int(settings.get("window_end", 9)) <= int(settings.get("window_start", 6)):
            raise ValueError("window_end 必须晚于 window_start")
        payload["settings"] = settings
        if not settings.get("enabled") or settings.get("mode") == "off":
            payload["meta"]["active_event"] = None
        self.save(payload)
        return self.state_view(advance=False)

    @_serialized
    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        self.ensure_schema()
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM morning_response_events ORDER BY id DESC LIMIT ?",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metrics"] = _json(item.pop("metrics_json", "{}"), {})
            item["levels"] = {
                key: _metric(float(item["metrics"].get(key, 0)))
                for key in METRIC_LABELS
            }
            result.append(item)
        return result

    @_serialized
    def dispatch_status(self, event_id: int) -> dict[str, Any]:
        self.ensure_schema()
        if int(event_id or 0) <= 0:
            return {}
        with get_db() as db:
            row = db.execute(
                """SELECT proactive_state, proactive_event_id,
                          proactive_session_id, proactive_queued_at,
                          proactive_finished_at, proactive_outcome
                   FROM morning_response_events WHERE id=?""",
                (int(event_id),),
            ).fetchone()
        return dict(row) if row else {}

    @_serialized
    def proactive_candidate(
        self,
        now: datetime | None = None,
        *,
        context: dict[str, Any] | None = None,
        advance: bool = True,
    ) -> dict[str, Any] | None:
        """Return the current undelivered morning event, never a stale catch-up."""
        payload = self.advance(now, context=context) if advance else self.load()
        settings = payload["settings"]
        if (
            not settings.get("enabled", True)
            or settings.get("mode") == "off"
            or not settings.get("proactive_enabled", True)
        ):
            return None
        event = deepcopy(payload["meta"].get("active_event"))
        if not isinstance(event, dict) or event.get("caught_up"):
            return None
        local = self._now(now, settings)
        until = _parse(event.get("until"))
        if until and local.astimezone(timezone.utc) >= until:
            return None
        event_id = int(event.get("event_id") or 0)
        if not event_id:
            return None
        dispatch = self.dispatch_status(event_id)
        # A retry already belongs to the original durable co-presence row.
        # Re-queuing it here could move one morning event into a newer window.
        if str(dispatch.get("proactive_state") or "pending") != "pending":
            return None
        event["levels"] = {
            key: _metric(float((event.get("metrics") or {}).get(key, 0)))
            for key in METRIC_LABELS
        }
        event["metric_labels"] = METRIC_LABELS
        event["dispatch"] = dispatch
        return event

    @_serialized
    def refresh_for_proactive(
        self,
        now: datetime | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Advance only when a morning event can change, then return a candidate."""
        payload = self.load()
        settings = payload["settings"]
        local = self._now(now, settings)
        date_key = local.date().isoformat()
        active = payload["meta"].get("active_event")
        last = _parse(payload["meta"].get("last_advanced_at"))
        stale_active = isinstance(active, dict) and (
            last is None
            or local.astimezone(timezone.utc) - last.astimezone(timezone.utc)
            >= timedelta(seconds=60)
        )
        event_not_drawn = (
            payload["meta"].get("event_date") != date_key
            and local >= self._event_time(payload, local)
        )
        if event_not_drawn or stale_active:
            self.advance(local, context=context)
        return self.proactive_candidate(local, context=context, advance=False)

    @_serialized
    def mark_proactive_queued(
        self,
        event_id: int,
        *,
        co_presence_event_id: int,
        session_id: str,
        now: datetime | None = None,
    ) -> bool:
        self.ensure_schema()
        current = self._now(now)
        with get_db() as db:
            cursor = db.execute(
                """UPDATE morning_response_events
                   SET proactive_state='queued', proactive_event_id=?,
                       proactive_session_id=?, proactive_queued_at=?,
                       proactive_finished_at='', proactive_outcome='已进入主动消息链路'
                   WHERE id=? AND proactive_state='pending'""",
                (
                    int(co_presence_event_id),
                    str(session_id or "")[:128],
                    _iso(current),
                    int(event_id),
                ),
            )
        return bool(cursor.rowcount)

    @_serialized
    def mark_proactive_outcome(
        self,
        event_id: int,
        *,
        state: str,
        outcome: str = "",
        co_presence_event_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        self.ensure_schema()
        safe_state = str(state or "queued")[:32]
        terminal = safe_state in {"delivered", "waited", "superseded", "error"}
        current = self._now(now)
        with get_db() as db:
            db.execute(
                """UPDATE morning_response_events
                   SET proactive_state=?,
                       proactive_event_id=COALESCE(?, proactive_event_id),
                       proactive_finished_at=?, proactive_outcome=?
                   WHERE id=?""",
                (
                    safe_state,
                    int(co_presence_event_id) if co_presence_event_id else None,
                    _iso(current) if terminal else "",
                    str(outcome or "")[:500],
                    int(event_id),
                ),
            )

    @_serialized
    def manual_test_event(
        self,
        now: datetime | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a current synthetic event for exercising the real delivery chain."""
        payload = self.load()
        local = self._now(now, payload["settings"])
        metrics = self._initial_metrics(payload, local, context)
        duration = max(
            30, min(180, int(payload["settings"].get("duration_minutes", 90)))
        )
        event = {
            "event_id": 0,
            "event_date": local.date().isoformat(),
            "label": f"晨间主动触发测试 · Lv.{_metric(metrics['hardness'])}",
            "metrics": metrics,
            "description": self._description(metrics),
            "occurred_at": _iso(local),
            "last_updated_at": _iso(local),
            "until": _iso(local + timedelta(minutes=duration)),
            "caught_up": False,
            "manual_test": True,
        }
        event["levels"] = {
            key: _metric(float(metrics.get(key, 0))) for key in METRIC_LABELS
        }
        event["metric_labels"] = METRIC_LABELS
        return event

    @_serialized
    def reset(self, *, keep_settings: bool = True, now: datetime | None = None) -> dict[str, Any]:
        old = self.load()
        payload = self._defaults(now)
        if keep_settings:
            payload["settings"] = deepcopy(old["settings"])
        payload["meta"]["legacy_migrated"] = True
        self._cache = payload
        self.save(payload)
        return self.state_view(now=now, advance=False)

    @_serialized
    def prompt_context(self) -> str:
        view = self.state_view()
        event = view.get("active")
        if not isinstance(event, dict):
            return ""
        levels = event.get("levels") or {}
        return "\n".join((
            "<morning_response>",
            "当前存在独立的晨间生理反应；它不是性欲的同义词。",
            (
                f"硬度 {levels.get('hardness', 1)}/10，敏感 {levels.get('sensitivity', 1)}/10，"
                f"主观性欲 {levels.get('desire', 1)}/10，身体张力 {levels.get('physical_tension', 1)}/10，"
                f"自控力 {levels.get('self_control', 1)}/10，身体夺权 {levels.get('body_takeover', 1)}/10。"
            ),
            str(event.get("description") or ""),
            "只在当前话题相关时让它影响注意力、动作倾向与措辞；不要机械朗读数值。",
            "</morning_response>",
        ))

    @_serialized
    def health(self) -> dict[str, Any]:
        try:
            view = self.state_view()
            active = view.get("active")
            detail = (
                f"{active.get('label')} · 独立七指标"
                if isinstance(active, dict)
                else f"等待下一次晨间窗口 · {view['settings'].get('mode', 'natural')}"
            )
            return {"health": "ok", "detail": detail}
        except Exception as exc:
            return {"health": "error", "detail": "晨间响应组件暂不可用"}


morning_response = MorningResponseEngine()
