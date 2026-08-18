"""
desire_host.py — 欲望系统宿主适配层
内核(desire_engine)零IO，这里只保留旧状态的持久化、墙上时间→tick
与兼容面板。模型行为、主动联系和当前聊天状态已经统一由每会话
canonical state 决定；旧全局单例不得再向 proactive/推送链路分发。
"""
import json
import random
from datetime import datetime

from config import DESIRE_GATES, PROACTIVE_CONFIG
from models import get_db
from desire_engine import (
    DesireState, Thought, P, pulse_owner, pulse_self_experience,
    add_thought, tick, scores, pick_intent,
    heartbeat_multiplier, total_tension,
)


# 这轮大西瓜还没有外向动作执行器（逛代码/查世界/逛社交要接外网工具），
# 先列为 blocked —— wildcard 的"最高的做不了(卡死)"分支会自然兜住
BLOCKED_ACTIONS = {"explore_code", "explore_world", "browse_social", "read_book"}

class DesireHost:
    def __init__(self):
        self._st: DesireState | None = None

    # ── 持久化 ──
    def _ensure_table(self):
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS desire_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")

    def load(self) -> DesireState:
        if self._st is not None:
            return self._st
        self._ensure_table()
        with get_db() as db:
            row = db.execute(
                "SELECT state_json FROM desire_state WHERE id = 1").fetchone()
        if row:
            d = json.loads(row["state_json"])
            thoughts = [Thought(**t) for t in d.pop("thoughts", [])]
            st = DesireState(**{k: v for k, v in d.items()
                                if k in DesireState.__dataclass_fields__})
            st.thoughts = thoughts
            self._st = st
        else:
            self._st = DesireState()
        return self._st

    def save(self):
        if self._st is None:
            return
        self._ensure_table()
        with get_db() as db:
            db.execute(
                """INSERT INTO desire_state (id, state_json, updated_at)
                   VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                   state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (json.dumps(self._st.snapshot(), ensure_ascii=False),
                 datetime.now().isoformat()))

    # ── 事件入口（app.py 聊天时调）──
    def on_owner_message(self, text: str):
        """主人说话：快通道 pulse + 消息片段入念头池"""
        st = self.load()
        pulse_owner(st)
        if text and len(text.strip()) >= 6:
            add_thought(st, text.strip()[:120], "attachment", 0.4)
        self.save()

    def on_self_material(self, drive_key: str, text: str, theme: str = ""):
        """它自己拿到素材（外向动作的回填口，executor 接好后调这里）"""
        if not DESIRE_GATES.get("DESIRE_SELF_DRIVE"):
            return
        st = self.load()
        pulse_self_experience(st, drive_key, text[:80])
        add_thought(st, text, drive_key, 0.5, theme=theme)
        st.self_drive_today += 1
        self.save()

    def apply_deltas(
        self,
        deltas: dict[str, float],
        *,
        source: str = "inner_life",
        thought: str = "",
    ) -> dict[str, float]:
        """Validated coupling entrance used by the unified inner-life runtime."""
        st = self.load()
        applied: dict[str, float] = {}
        for key, raw_delta in (deltas or {}).items():
            if key not in st.drive or key.startswith("_"):
                continue
            delta = max(-0.24, min(0.24, float(raw_delta)))
            before = float(st.drive[key])
            st.drive[key] = max(0.0, min(1.0, before + delta))
            actual = st.drive[key] - before
            if abs(actual) >= 0.0005:
                applied[key] = round(actual, 4)
        if thought and applied:
            strongest = max(applied, key=lambda key: abs(applied[key]))
            add_thought(
                st,
                str(thought)[:200],
                strongest,
                min(0.88, 0.45 + abs(applied[strongest])),
                theme=f"{source}:{strongest}"[:80],
            )
        self.save()
        return applied

    # ── 心跳一拍（端到端闭环，文档第8节）──
    async def heartbeat(self) -> dict | None:
        """
        让旧兼容档案继续缓慢演化，但永远不产生外向动作。

        历史版本的 ``DESIRE_DRIVEN`` 能让这个进程级单例直接排队发言，
        它既没有 session 所有权，也绕过了当前模型的 canonical snapshot。
        保留该行为会再次造成“三合一是三合一，模型是模型”，因此即使
        旧环境变量仍为 true，这里也只保存档案并返回 None。
        """
        st = self.load()
        g = {
            "coupling": DESIRE_GATES.get("DESIRE_COUPLING", False),
            "baseline_drift": DESIRE_GATES.get("DESIRE_BASELINE_DRIFT", False),
            "self_drive": DESIRE_GATES.get("DESIRE_SELF_DRIVE", False),
        }
        tick(st, gates=g)

        self.save()
        return None

    async def _execute(self, intent: dict) -> bool:
        """Compatibility tombstone: global intents cannot own a chat window."""
        return False

    # ── 心跳间隔（HEARTBEAT_AUTONOMY）──
    def next_interval_minutes(self, base_minutes: int) -> float:
        """张力↔间隔。勿扰时段 floor：不让它缩短间隔去打扰。"""
        if not DESIRE_GATES.get("HEARTBEAT_AUTONOMY"):
            return base_minutes
        st = self.load()
        mult = heartbeat_multiplier(st)
        now_h = datetime.now().hour
        qs, qe = PROACTIVE_CONFIG.get("quiet_hours", (1, 8))
        if qs <= now_h < qe:
            mult = max(mult, 1.0)                          # 安静时段只许拉长
        return base_minutes * mult

    # ── 只读状态（gated 也能看）──
    def state_view(self) -> dict:
        st = self.load()
        intent = pick_intent(st, random.Random(0), blocked_actions=BLOCKED_ACTIONS)
        public_gates = dict(DESIRE_GATES)
        public_gates["DESIRE_DRIVEN"] = False
        public_gates["CANONICAL_ONLY"] = True
        return {
            "drive": {k: round(st.drive[k], 3) for k in
                      ("attachment", "curiosity", "reflection", "duty",
                       "social", "fatigue", "libido", "stress")},
            "scores": scores(st),
            "tension": round(total_tension(st), 3),
            "intent": intent,
            "thoughts": [
                {"text": t.text, "drive": t.drive, "kind": t.kind,
                 "strength": round(t.strength, 3), "fed": t.fed_count}
                for t in sorted(st.thoughts, key=lambda x: -x.strength)[:12]],
            "self_drive": {
                "enabled": DESIRE_GATES.get("DESIRE_SELF_DRIVE", False),
                "curiosity_floor": round(st.curiosity_floor, 3),
                "today_count": st.self_drive_today,
                "last_pulse": st.last_self_pulse,
            },
            "attach_floor": round(st.attach_floor, 3),
            "tick": st.tick,
            "gates": public_gates,
            "legacy_outbound_retired": True,
        }


desire_host = DesireHost()
