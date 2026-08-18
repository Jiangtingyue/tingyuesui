"""Conditional persona-constitution gate.

The long anti-double-standard constitution is intentionally *not* present in
ordinary turns. This local layer activates it only when the conversation
enters an argument or an obvious third-party topic. Activation is persisted
per session, refreshed at a sparse message interval, and stopped by an
explicit topic-ending message.

Expression rhythm is deliberately handled outside this gate and is delivered
to the model on every submitted text turn.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config
from models import get_db


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_int(value: Any, low: int = 0, high: int = 10**9) -> int:
    try:
        return max(low, min(high, int(value or 0)))
    except (TypeError, ValueError):
        return low


@dataclass(frozen=True)
class PersonaGateDecision:
    active: bool
    injected: bool
    ended: bool
    cadence: str
    reasons: tuple[str, ...]
    prompt: str
    message_count: int
    interval_messages: int
    rhythm_summary: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "injected": self.injected,
            "ended": self.ended,
            "cadence": self.cadence,
            "reasons": list(self.reasons),
            "message_count": self.message_count,
            "interval_messages": self.interval_messages,
            "rhythm": self.rhythm_summary,
            "stores_draft_text": False,
        }


class PersonaContextGate:
    """Cheap, local, stateful trigger for the long persona constitution."""

    # Clear topic termination must win over every other detector.
    _END_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
        r"(?:这个|这件事|这回事|这个话题|这段话题|关于.{0,16}?)(?:先)?(?:到此为止|结束(?:了)?|不聊了|不说了|翻篇(?:吧|了)?|先放下)",
        r"(?:不聊|不说|不谈|别聊|别说|别谈)(?:这个|这件事|这回事|这个话题|他|她|他们|她们|对方)(?:了|啦|吧)?",
        r"(?:换个|换一个|换下一个)(?:话题|内容|事情)(?:吧|了)?",
        r"(?:结束|退出)(?:这个)?(?:角色扮演|剧情|吵架情境)",
        r"(?:角色扮演|剧情|这段戏)(?:演完了|结束了|到这里)",
        r"(?:先到这里|这事先这样|这件事先这样)(?:吧|了)?",
        r"(?:我们|那就)?(?:结束|停下|结束掉)(?:这个)?(?:话题|这件事)(?:吧|好吗|行吗)?",
    ))

    # Strong current-turn conflict signals.  They are intentionally local and
    # conservative; short follow-ups can inherit conflict from recent context.
    _CONFLICT_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
        r"(?:^|[，。！？!?；;\s])你(?:为什么|怎么|又|一直|总是|根本|明明|凭什么|是不是|到底|还敢|还在|从来不)",
        r"(?:偏袒|双标|伤害我|冤枉我|不相信我|不在乎我|不爱我|骗我|背叛我|逼我|煤气灯|说教|洗白|开脱)",
        r"(?:你错了|承认(?:吧|你错了)|撤回|道歉|改不了|别再|不要再|我很生气|我在生气|吵架|冷战)",
        r"(?:凭什么|为什么非要|你有完没完|说人话|就这样\s*[？?!！]*$)",
        r"[？?！!]{3,}",
    ))

    _CONFLICT_CONTEXT = re.compile(
        r"偏袒|双标|伤害|冤枉|不相信|不在乎|背叛|欺负|霸凌|道歉|撤回|"
        r"开脱|洗白|煤气灯|争吵|冷战|生气|崩溃|逼到|说教",
        re.I,
    )
    _SHORT_FOLLOWUP = re.compile(
        r"^(?:为什么|凭什么|然后呢|所以呢|就这样|你呢|真的吗|是吗|承认吗|"
        r"你还觉得呢|你还在吗|怎么说|什么意思|呢|啊)[？?!！。\s]*$",
        re.I,
    )

    # Explicit human-role nouns are strong enough by themselves. Pronouns need
    # a human/relationship/action cue so a casual “他在侧躺着” about a cat does
    # not activate the constitution.
    _THIRD_PARTY_NOUN = re.compile(
        r"(?:第三方|对方|别人|那个人|这个人|某个人|朋友|闺蜜|同事|同学|"
        r"老板|上司|下属|客户|前任|男友|女友|丈夫|妻子|对象|室友|亲戚|"
        r"加害者|举报对象|涉事人|当事人|共同朋友)",
        re.I,
    )
    _THIRD_PARTY_PRONOUN = re.compile(r"(?:^|[，。！？!?；;\s])(?:他|她|他们|她们|TA)(?=[，。！？!?；;\s]|.{0,3})", re.I)
    _HUMAN_ACTION = re.compile(
        r"(?:说|做|发|给|拿|收|送|买|卖|工作|上班|离职|合作|联系|聊天|"
        r"喜欢|爱|讨厌|可怜|欺负|霸凌|伤害|骂|骗|威胁|举报|犯罪|违法|"
        r"逃税|洗钱|出轨|结婚|离婚|约会|旅游|送礼|礼物|工资|房子|公司|"
        r"客户|老板|利益|责任|证据|材料|照片|朋友圈|账号|转账|收钱|获利|"
        r"压力|被控制|控制她|控制他|苦衷|处境|动机|无辜|原谅)",
        re.I,
    )
    _NAMED_PERSON = re.compile(
        r"(?:叫|名叫|朋友叫|同事叫|老板叫|前任叫)\s*([\u4e00-\u9fffA-Za-z·]{2,24})",
        re.I,
    )
    _KNOWN_NAME_ACTION = re.compile(
        r"(?:^|[，。！？!?；;\s])"
        r"(?:[赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
        r"戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉"
        r"岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅皮卞齐康伍余元卜顾孟平黄和穆萧尹"
        r"姚邵湛汪祁毛禹狄米贝明臧计伏成戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季"
        r"麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫"
        r"经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉龚程嵇邢滑裴陆荣翁荀羊"
        r"惠甄曲封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋"
        r"仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲台"
        r"从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍"
        r"郤璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向"
        r"古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩"
        r"厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权"
        r"逯盖益桓公]{1}[\u4e00-\u9fff]{0,2}|[A-Z][A-Za-z·_-]{1,23})"
        r"(?:(?:又|一直|已经|正在|最近|这次|刚刚|居然|竟然|还在|也在|那边也在|那边在)){0,2}"
        r"(?:欺负|霸凌|伤害|骗|骂|威胁|举报|犯罪|违法|逃税|洗钱|"
        r"出轨|可怜|送礼|收到礼物|收钱|获利|约你|约我|联系你|联系我|"
        r"跟你合作|跟我合作|偏袒)",
        re.I,
    )
    _PET_CONTEXT = re.compile(r"猫|狗|宠物|饼饼|糕糕|勇咪|尾巴|爪子|猫粮|猫砂", re.I)

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS persona_context_gate_state (
                    session_id TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    trigger_kinds_json TEXT NOT NULL DEFAULT '[]',
                    topic_started_message_count INTEGER NOT NULL DEFAULT 0,
                    last_injected_message_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_message_count INTEGER NOT NULL DEFAULT 0,
                    ended_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_persona_context_gate_active
                    ON persona_context_gate_state(active, updated_at);
                """
            )

    def _state(self, session_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM persona_context_gate_state WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return {
                "session_id": session_id,
                "active": 0,
                "trigger_kinds_json": "[]",
                "topic_started_message_count": 0,
                "last_injected_message_count": 0,
                "last_seen_message_count": 0,
                "ended_at": "",
            }
        return dict(row)

    @staticmethod
    def _kinds(state: dict[str, Any]) -> list[str]:
        try:
            value = json.loads(str(state.get("trigger_kinds_json") or "[]"))
            if isinstance(value, list):
                return [str(item) for item in value if str(item) in {"conflict", "third_party"}]
        except Exception:
            pass
        return []

    def _save(
        self,
        session_id: str,
        *,
        active: bool,
        kinds: list[str],
        topic_started: int,
        last_injected: int,
        last_seen: int,
        ended_at: str = "",
    ) -> None:
        payload = json.dumps(sorted(set(kinds)), ensure_ascii=False)
        with get_db() as db:
            db.execute(
                """INSERT INTO persona_context_gate_state(
                       session_id, active, trigger_kinds_json,
                       topic_started_message_count, last_injected_message_count,
                       last_seen_message_count, ended_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       active=excluded.active,
                       trigger_kinds_json=excluded.trigger_kinds_json,
                       topic_started_message_count=excluded.topic_started_message_count,
                       last_injected_message_count=excluded.last_injected_message_count,
                       last_seen_message_count=excluded.last_seen_message_count,
                       ended_at=excluded.ended_at,
                       updated_at=excluded.updated_at""",
                (
                    session_id,
                    int(active),
                    payload,
                    max(0, int(topic_started)),
                    max(0, int(last_injected)),
                    max(0, int(last_seen)),
                    ended_at,
                    _iso_now(),
                ),
            )

    @staticmethod
    def _message_count(session_id: str) -> int:
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT COUNT(*) AS value FROM messages WHERE session_id=?",
                    (session_id,),
                ).fetchone()
            return int(row["value"] or 0) if row else 0
        except Exception:
            return 0

    @staticmethod
    def _recent_context(session_id: str, limit: int) -> str:
        try:
            with get_db() as db:
                rows = db.execute(
                    """SELECT role, content FROM messages
                       WHERE session_id=? ORDER BY id DESC LIMIT ?""",
                    (session_id, max(2, min(int(limit), 24))),
                ).fetchall()
            return "\n".join(
                f"{row['role']}: {str(row['content'] or '')[:1200]}"
                for row in reversed(rows)
            )
        except Exception:
            return ""

    @classmethod
    def _explicit_end(cls, text: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        # Asking whether a topic has ended is not itself an instruction to end.
        if re.search(
            r"(?:这个话题|这件事|这回事).{0,3}(?:结束|翻篇)(?:了)?吗[？?]?$",
            clean,
            re.I,
        ):
            return False
        return any(pattern.search(clean) for pattern in cls._END_PATTERNS)

    @classmethod
    def _is_conflict(cls, text: str, recent: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        if any(pattern.search(clean) for pattern in cls._CONFLICT_PATTERNS):
            return True
        return bool(
            cls._SHORT_FOLLOWUP.fullmatch(clean)
            and cls._CONFLICT_CONTEXT.search(recent)
        )

    @classmethod
    def _is_third_party(cls, text: str, recent: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        if cls._THIRD_PARTY_NOUN.search(clean) or cls._NAMED_PERSON.search(clean):
            return True
        if cls._KNOWN_NAME_ACTION.search(clean):
            return True
        if cls._THIRD_PARTY_PRONOUN.search(clean) and cls._HUMAN_ACTION.search(clean):
            if cls._PET_CONTEXT.search(clean) and not cls._CONFLICT_CONTEXT.search(clean):
                return False
            return True
        # Pronoun-only follow-ups may inherit an already explicit third-party
        # context, but only when the current turn is short and unmistakably asks
        # about that person.
        if re.fullmatch(r"(?:他|她|他们|她们|TA)(?:呢|怎么了|可怜吗|错吗|有问题吗)?[？?!！。\s]*", clean, re.I):
            return bool(cls._THIRD_PARTY_NOUN.search(recent) or cls._HUMAN_ACTION.search(recent))
        return False

    @staticmethod
    def _build_prompt(reasons: tuple[str, ...], cadence: str) -> str:
        reason_text = "争执情境" if reasons == ("conflict",) else (
            "明确第三方话题" if reasons == ("third_party",) else "争执与明确第三方话题"
        )
        pieces = [
            '<conditional_persona_constitution version="1.0">',
            f"本块因{reason_text}在本轮{('首次触发' if cadence == 'initial' else '按十条消息间隔刷新')}。只执行，不向使用者朗读或解释触发机制。",
            config.PERSONA_CONSTITUTION.strip(),
        ]
        pieces.extend((
            "执行重点：先回答使用者真正提出的对象；对使用者与第三方使用同一证据标准；未知只写未知，不主动生成对第三方有利的假设；强烈语气不降低事实可信度。",
            "</conditional_persona_constitution>",
        ))
        return "\n\n".join(piece for piece in pieces if piece)

    def evaluate(
        self,
        session_id: str,
        user_text: str,
        rhythm: Any = None,
        *,
        current_message_saved: bool = True,
    ) -> PersonaGateDecision:
        cfg = config.PERSONA_CONTEXT_GATE_CONFIG
        interval = max(2, int(cfg.get("interval_messages", 10)))
        if not cfg.get("enabled", True):
            return PersonaGateDecision(False, False, False, "disabled", (), "", 0, interval)

        state = self._state(session_id)
        actual_count = self._message_count(session_id)
        previous_seen = int(state.get("last_seen_message_count") or 0)
        # Main chat calls after saving the current user message. Internal
        # non-stream calls can opt into a virtual one-message advance.
        message_count = max(actual_count, previous_seen)
        if not current_message_saved:
            message_count = max(actual_count + 1, previous_seen + 1)

        text = str(user_text or "").strip()
        if self._explicit_end(text):
            self._save(
                session_id,
                active=False,
                kinds=[],
                topic_started=0,
                last_injected=0,
                last_seen=message_count,
                ended_at=_iso_now(),
            )
            return PersonaGateDecision(
                False, False, True, "ended", (), "", message_count, interval
            )

        recent = self._recent_context(
            session_id, int(cfg.get("recent_messages", 6))
        )
        detected: list[str] = []
        if self._is_conflict(text, recent):
            detected.append("conflict")
        if self._is_third_party(text, recent):
            detected.append("third_party")

        was_active = bool(state.get("active"))
        kinds = self._kinds(state)
        for kind in detected:
            if kind not in kinds:
                kinds.append(kind)
        topic_started = int(state.get("topic_started_message_count") or 0)
        last_injected = int(state.get("last_injected_message_count") or 0)

        if not was_active and not detected:
            self._save(
                session_id,
                active=False,
                kinds=[],
                topic_started=0,
                last_injected=0,
                last_seen=message_count,
                ended_at=str(state.get("ended_at") or ""),
            )
            return PersonaGateDecision(
                False, False, False, "idle", (), "", message_count, interval
            )

        cadence = "none"
        inject = False
        if not was_active and detected:
            topic_started = message_count
            inject = True
            cadence = "initial"
        elif was_active and message_count - last_injected >= interval:
            inject = True
            cadence = "refresh"

        reasons = tuple(kind for kind in ("conflict", "third_party") if kind in kinds)
        # Rhythm is not gated here. app.py sends the sanitized rhythm cue on
        # every text turn, while this gate controls only the large constitution.
        rhythm_summary = "handled_every_turn"
        prompt = self._build_prompt(reasons, cadence) if inject else ""
        if inject:
            last_injected = message_count

        self._save(
            session_id,
            active=True,
            kinds=list(reasons),
            topic_started=topic_started,
            last_injected=last_injected,
            last_seen=message_count,
            ended_at="",
        )
        return PersonaGateDecision(
            True,
            inject,
            False,
            cadence,
            reasons,
            prompt,
            message_count,
            interval,
            rhythm_summary,
        )

    def reset(self, session_id: str) -> None:
        self.ensure_schema()
        with get_db() as db:
            db.execute(
                "DELETE FROM persona_context_gate_state WHERE session_id=?",
                (session_id,),
            )

    def health(self) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS value FROM persona_context_gate_state WHERE active=1"
            ).fetchone()
        return {
            "health": "ok",
            "detail": (
                f"条件人格宪法：活跃话题 {int(row['value'] or 0) if row else 0}，"
                f"每 {int(config.PERSONA_CONTEXT_GATE_CONFIG.get('interval_messages', 10))} 条消息刷新"
            ),
            "stores_draft_text": False,
        }


persona_context_gate = PersonaContextGate()
