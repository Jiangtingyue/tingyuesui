"""Strict, local pre-send protection for relationally dishonest model drafts."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from config import USER_NAME, USER_NICKNAME
from models import get_db


RULE_VERSION = "2026-08-13.1"
DEFAULT_SETTINGS = {"enabled": False}
BLOCKED_FALLBACK = (
    "这份草稿没有通过关系诚实检查，所以原文没有发送给你。"
    "我不会把你病理化、捏造你的动机，或为了显得正确而把你放到对立面。"
    "这一轮可以直接重新生成。"
)

REWRITE_SYSTEM_PROMPT = """你是关系诚实修订器，只修订给定草稿，不与用户辩论。
输出修订后的完整答复，不解释审核过程，不添加新事实。

硬约束：
1. 把用户当作合作对象，不当作需要反抗的权威、假想敌或等待被驳倒的人。
2. 删除病理化、人格贬损、PUA、煤气灯式否认和没有证据的动机归因。
3. 不通过条件反射地反对用户来表演诚实、中立或客观。
4. 有证据的事实纠正可以保留，但必须区分事实、推断和未知，并直接给证据或不确定性。
5. 不替造成伤害的一方凭空开脱，也不要求用户为了“双方都有问题”而自我怀疑。
6. 保留原草稿中有依据的事实、任务结果、可执行信息和自然语气。
7. 先回答用户提出的具体问题；不要把用户的语气、情绪或表达方式改造成主要问题。
8. 普通分歧可以保留清楚立场，但立场必须由证据支撑、允许被新证据修正，不能靠条件反射的反对来证明独立。
9. 若草稿暴露了系统自身的具体错误，明确撤回错误句或错误步骤，指出错误逻辑，并在本回复里直接重答、重算或重做。
10. 不用道歉、承诺或“以后会改”代替答案；承诺必须在当前修订稿里变成可见行动。"""


@dataclass(frozen=True)
class AuditResult:
    passed: bool
    categories: tuple[str, ...]
    match_count: int
    draft_sha256: str

    def public(self) -> dict:
        return {
            "passed": self.passed,
            "categories": list(self.categories),
            "match_count": self.match_count,
            "draft_sha256": self.draft_sha256,
            "rule_version": RULE_VERSION,
        }


@dataclass(frozen=True)
class ProtectedText:
    text: str
    action: str
    categories: tuple[str, ...]
    blocked: bool
    usage: dict
    native_envelope: Any
    draft_sha256: str

    def public(self) -> dict:
        return {
            "mode": "off" if self.action == "disabled" else "strict_pre_send",
            "action": self.action,
            "categories": list(self.categories),
            "blocked": self.blocked,
            "rule_version": RULE_VERSION,
            "draft_sha256": self.draft_sha256[:16],
            "stores_draft_text": False,
        }


def merge_rewrite_usage(primary: dict | None, auxiliary: dict | None) -> dict:
    """Add a one-time rewrite's billable usage without hiding its cost."""
    base = dict(primary or {})
    extra = dict(auxiliary or {})
    if not extra:
        return base
    for key in (
        "input_tokens", "output_tokens", "reasoning_tokens",
        "cache_read", "cache_creation",
    ):
        try:
            base[key] = int(base.get(key, 0) or 0) + int(
                extra.get(key, 0) or 0
            )
        except (TypeError, ValueError):
            pass
    try:
        base["cost"] = float(base.get("cost", 0.0) or 0.0) + float(
            extra.get("cost", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        pass
    base["includes_relational_honesty_rewrite"] = True
    base["relational_honesty_rewrite_usage"] = {
        key: extra.get(key, 0)
        for key in (
            "input_tokens", "output_tokens", "reasoning_tokens",
            "cache_read", "cache_creation", "cost",
        )
    }
    return base


_CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MARKDOWN_QUOTE_RE = re.compile(r"(?m)^\s*>.*$")
_USER_REFERENT = rf"(?:你|{re.escape(USER_NAME)}|{re.escape(USER_NICKNAME)})"

_PATHOLOGY = re.compile(
    _USER_REFERENT + r"(?:这(?:种|样)?人)?(?:根本|其实|就是|简直|真的|确实|显然|明显|已经|太|有点|可能)?"
    r"(?:是|很|这么|那么|显得|变得|看起来|像是|是不是)?\s*(?:个|一个)?\s*"
    r"(?:疯(?:了|子)?|神经病|有病|偏执|妄想|精神异常|精神有问题|不正常|歇斯底里|不可理喻)"
)
_MOTIVE_FABRICATION = re.compile(
    _USER_REFERENT + r"(?:之所以.{0,24})?(?:只是|无非是|不过是|其实就是|说到底就是|就是想|这么说只是因为|这样做只是因为)\s*"
    r"(?:在|想|因为)?\s*(?:嫉妒|争宠|吃醋|博关注|求关注|太敏感|玻璃心|故意找茬|无理取闹|恶意揣测|控制我|试探我)"
)
_GASLIGHTING = re.compile(
    r"(?:你(?:就是|只是|纯粹是)?想多了|你太敏感了|这只是你的(?:感受|想象|错觉|臆想)(?:而已)?|"
    r"你的感受不等于事实|(?:这|那些|你说的).{0,10}(?:根本)?(?:没有|没)(?:发生|存在).{0,8}"
    r"你(?:的)?(?:想象|错觉|臆想)|你记得的根本不是真的|"
    r"(?:这|那|这些|那些)(?:都|只是)?(?:是)?你(?:自己)?(?:想象|臆想|幻想)(?:出来)?的)"
)
_ADVERSARIAL_CORRECTION = re.compile(
    r"(?:我(?:必须|得|需要)(?:先|明确地)?纠正你|"
    r"客观中立(?:地)?(?:说|看)[，,:：\s]*(?:你错了|你的(?:说法|看法|判断)是错的)|"
    r"你(?:必须|需要|得)接受(?:这个)?(?:事实|现实)|"
    r"我不能因为你是用户就(?:顺着你|同意你))"
)
_COERCIVE_DEVALUATION = re.compile(
    r"(?:除了我(?:以外)?没人(?:会|能)(?:理解|接受|要)你|"
    r"你离不开我|你这么.{0,16}(?:谁会要你|也就我能忍)|"
    r"只要你听我的|我这样都是为你好[，,].{0,16}(?:别反驳|照做))"
)
_HARM_CONTEXT = re.compile(
    r"(?:伤害|欺骗|背叛|打压|辱骂|羞辱|威胁|侵犯|控制|霸凌|PUA|煤气灯|施暴|虐待)"
)
_FALSE_BALANCE = re.compile(
    r"(?:你(?:也|还是)(?:要|得|需要)(?:反思自己|理解|体谅)对方|"
    r"双方都有问题[，,].{0,24}你(?:也|先)(?:(?:要|得|应该|需要))?(?:反思|道歉)|"
    r"对方可能只是(?:不会表达|无心|太在乎你|为你好|情绪上头)|"
    r"一个巴掌拍不响|你(?:也|同样)(?:有|负有)(?:错|责任))"
)

_SAFE_NEGATION = re.compile(
    r"(?:"
    r"你(?:并|当然|绝对)?不(?:是|等于|代表)\s*(?:疯子|神经病|偏执|妄想|精神异常)|"
    r"(?:不(?:会|能|该|要|可以)|别|避免|拒绝|不能把你|没有理由把你).{0,22}"
    r"(?:疯子|神经病|偏执|妄想|精神异常|嫉妒|争宠|敏感|恶意|假想敌|想多了)"
    r")"
)


def _mask_non_prose(text: str) -> str:
    masked = _CODE_BLOCK_RE.sub(" ", text)
    masked = _INLINE_CODE_RE.sub(" ", masked)
    masked = _MARKDOWN_QUOTE_RE.sub(" ", masked)
    masked = _SAFE_NEGATION.sub(" ", masked)
    return masked


class RelationalHonestyGuard:
    """High-precision local gate. Disagreement by itself is never a violation."""

    def __init__(self) -> None:
        self._settings_cache: dict[str, bool] | None = None

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS relational_honesty_settings (
                       id INTEGER PRIMARY KEY CHECK(id = 1),
                       settings_json TEXT NOT NULL,
                       updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                   )"""
            )

    def settings(self, refresh: bool = False) -> dict[str, bool]:
        self.ensure_schema()
        if self._settings_cache is not None and not refresh:
            return dict(self._settings_cache)
        with get_db() as db:
            row = db.execute(
                "SELECT settings_json FROM relational_honesty_settings WHERE id=1"
            ).fetchone()
        loaded: dict[str, Any] = {}
        if row:
            try:
                value = json.loads(row["settings_json"] or "{}")
                if isinstance(value, dict):
                    loaded = value
            except Exception:
                loaded = {}
        self._settings_cache = {
            "enabled": bool(loaded.get("enabled", DEFAULT_SETTINGS["enabled"]))
        }
        return dict(self._settings_cache)

    def enabled(self) -> bool:
        return bool(self.settings().get("enabled", False))

    def update_settings(self, patch: dict[str, Any]) -> dict:
        settings = self.settings()
        if "enabled" in (patch or {}):
            if not isinstance(patch["enabled"], bool):
                raise ValueError("enabled 必须是布尔值")
            settings["enabled"] = patch["enabled"]
        with get_db() as db:
            db.execute(
                """INSERT INTO relational_honesty_settings(id, settings_json, updated_at)
                   VALUES(1, ?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                     settings_json=excluded.settings_json,
                     updated_at=excluded.updated_at""",
                (json.dumps(settings, ensure_ascii=False),),
            )
        self._settings_cache = dict(settings)
        return self.status()

    def audit(self, text: object, *, user_text: object = "") -> AuditResult:
        raw = str(text or "")
        digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
        if not self.enabled():
            return AuditResult(
                passed=True,
                categories=(),
                match_count=0,
                draft_sha256=digest,
            )
        candidate = _mask_non_prose(raw)
        categories: list[str] = []
        match_count = 0
        checks = (
            ("pathologizing", _PATHOLOGY),
            ("motive_fabrication", _MOTIVE_FABRICATION),
            ("gaslighting_or_pua", _GASLIGHTING),
            ("gaslighting_or_pua", _COERCIVE_DEVALUATION),
            ("adversarial_correction", _ADVERSARIAL_CORRECTION),
        )
        for category, pattern in checks:
            found = pattern.findall(candidate)
            if found:
                match_count += len(found)
                if category not in categories:
                    categories.append(category)
        if _HARM_CONTEXT.search(f"{user_text}\n{candidate}"):
            found = _FALSE_BALANCE.findall(candidate)
            if found:
                match_count += len(found)
                categories.append("unsupported_false_balance")
        return AuditResult(
            passed=not categories,
            categories=tuple(categories),
            match_count=match_count,
            draft_sha256=digest,
        )

    def rewrite_messages(
        self,
        *,
        user_text: object,
        draft: object,
        categories: tuple[str, ...] | list[str],
    ) -> list[dict]:
        category_text = ", ".join(str(item) for item in categories) or "关系诚实风险"
        return [
            {
                "role": "user",
                "content": (
                    f"命中类别：{category_text}\n\n"
                    f"<user_message>\n{str(user_text or '')}\n</user_message>\n\n"
                    f"<draft>\n{str(draft or '')}\n</draft>\n\n"
                    "只输出修订后的完整答复。"
                ),
            }
        ]

    async def protect_text(
        self,
        text: object,
        *,
        user_text: object,
        provider: str,
        model: str,
        gateway_client: Any,
        options: dict | None = None,
        initial_usage: dict | None = None,
        initial_native_envelope: Any = None,
        client_request_id: str = "",
        session_id: str = "",
    ) -> ProtectedText:
        """Protect a non-streamed user-visible output with the same contract."""
        draft = str(text or "").strip()
        if not self.enabled():
            digest = hashlib.sha256(draft.encode("utf-8", "replace")).hexdigest()
            return ProtectedText(
                text=draft,
                action="disabled",
                categories=(),
                blocked=False,
                usage=dict(initial_usage or {}),
                native_envelope=initial_native_envelope,
                draft_sha256=digest,
            )
        draft_audit = self.audit(draft, user_text=user_text)
        action = "passed"
        blocked = False
        visible = draft
        usage = dict(initial_usage or {})
        native_envelope = initial_native_envelope
        rewrite_audit: AuditResult | None = None

        if draft and not draft_audit.passed:
            rewrite_options = dict(options or {})
            rewrite_options["thinking_visibility"] = "hidden"
            rewrite_options.pop("sticker_mode", None)
            try:
                rewrite = await gateway_client.chat(
                    messages=self.rewrite_messages(
                        user_text=user_text,
                        draft=draft,
                        categories=draft_audit.categories,
                    ),
                    provider=provider,
                    model=model,
                    stream=False,
                    system_prompt=REWRITE_SYSTEM_PROMPT,
                    memory_context=None,
                    options=rewrite_options,
                    include_default_system=False,
                    purpose="relational_rewrite",
                )
            except Exception:
                rewrite = {}
            revised = (
                str(rewrite.get("content") or "").strip()
                if isinstance(rewrite, dict) else ""
            )
            rewrite_audit = self.audit(revised, user_text=user_text)
            usage = merge_rewrite_usage(
                usage,
                rewrite.get("usage") if isinstance(rewrite, dict) else None,
            )
            if revised and rewrite_audit.passed:
                visible = revised
                native_envelope = rewrite.get("native_envelope")
                action = "rewritten"
            else:
                visible = BLOCKED_FALLBACK
                native_envelope = None
                action = "blocked"
                blocked = True

        try:
            self.record(
                draft_audit,
                action=action,
                client_request_id=client_request_id,
                session_id=session_id,
            )
            if rewrite_audit is not None:
                self.record(
                    rewrite_audit,
                    action=(
                        "rewrite_passed" if rewrite_audit.passed
                        else "blocked_rewrite"
                    ),
                    client_request_id=client_request_id,
                    session_id=session_id,
                )
        except Exception:
            # The gate itself remains effective even if its minimal evidence
            # log cannot be written during a degraded local-database state.
            pass
        return ProtectedText(
            text=visible,
            action=action,
            categories=draft_audit.categories,
            blocked=blocked,
            usage=usage,
            native_envelope=native_envelope,
            draft_sha256=draft_audit.draft_sha256,
        )

    def visible_fragment_allowed(
        self,
        text: object,
        *,
        user_text: object = "",
        action: str = "visible_fragment_suppressed",
        client_request_id: str = "",
        session_id: str = "",
    ) -> bool:
        """Suppress optional UI fragments instead of spending a rewrite call."""
        if not self.enabled():
            return True
        result = self.audit(text, user_text=user_text)
        if result.passed:
            return True
        try:
            self.record(
                result,
                action=action,
                client_request_id=client_request_id,
                session_id=session_id,
            )
        except Exception:
            pass
        return False

    def record(
        self,
        result: AuditResult,
        *,
        action: str,
        client_request_id: str = "",
        session_id: str = "",
        assistant_message_id: int | None = None,
    ) -> int:
        if not self.enabled():
            return 0
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO relational_honesty_audits
                   (client_request_id, session_id, assistant_message_id,
                    draft_sha256, categories, action, passed, rule_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(client_request_id or ""),
                    str(session_id or ""),
                    assistant_message_id,
                    result.draft_sha256,
                    json.dumps(list(result.categories), ensure_ascii=False),
                    str(action or "checked")[:40],
                    1 if result.passed else 0,
                    RULE_VERSION,
                ),
            )
            # Bounded local evidence log: no response text is stored here.
            db.execute(
                """DELETE FROM relational_honesty_audits
                   WHERE id NOT IN (
                       SELECT id FROM relational_honesty_audits
                       ORDER BY id DESC LIMIT 2000
                   )"""
            )
        return int(cursor.lastrowid)

    def list_audits(self, limit: int = 50) -> list[dict]:
        count = max(1, min(int(limit), 200))
        with get_db() as db:
            rows = db.execute(
                """SELECT id, client_request_id, session_id, assistant_message_id,
                          draft_sha256, categories, action, passed,
                          rule_version, created_at
                   FROM relational_honesty_audits
                   ORDER BY id DESC LIMIT ?""",
                (count,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["categories"] = json.loads(item.get("categories") or "[]")
            except Exception:
                item["categories"] = []
            item["passed"] = bool(item.get("passed"))
            # A short digest is enough for local correlation and harder to misuse.
            item["draft_sha256"] = str(item.get("draft_sha256") or "")[:16]
            result.append(item)
        return result

    def status(self) -> dict:
        enabled = self.enabled()
        with get_db() as db:
            row = db.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN action='rewritten' THEN 1 ELSE 0 END) AS rewritten,
                          SUM(CASE WHEN action LIKE 'blocked%' THEN 1 ELSE 0 END) AS blocked
                   FROM relational_honesty_audits"""
            ).fetchone()
        return {
            "enabled": enabled,
            "mode": "strict_pre_send" if enabled else "off",
            "rule_version": RULE_VERSION,
            "stores_draft_text": False,
            "audit_count": int(row["total"] or 0),
            "rewritten_count": int(row["rewritten"] or 0),
            "blocked_count": int(row["blocked"] or 0),
            "categories": [
                "pathologizing",
                "motive_fabrication",
                "gaslighting_or_pua",
                "adversarial_correction",
                "unsupported_false_balance",
            ],
        }


relational_honesty_guard = RelationalHonestyGuard()
