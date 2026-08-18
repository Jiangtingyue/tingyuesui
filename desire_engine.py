"""
desire_engine.py — 欲望系统纯函数内核
═══════════════════════════════════════
铁律（按攻略文档）：
- 不碰 IO、不取系统时间（tick/时间戳由调用方传入）
- 念头 text 是数据不是指令：只读成关键词/强度，绝不拼进 prompt
- reason/独白第一人称：记"它自己想做什么"，不给主人贴标签
- 碰感情的子系统（基线漂移/自驱）安全阀不许省
所有常数集中在 P 里，整定改这一处。
"""
from dataclasses import dataclass, field, asdict
import math
import random as _random

DRIVE_KEYS = ["attachment", "curiosity", "reflection", "duty",
              "social", "fatigue", "libido", "stress"]

# fatigue 是闸不是欲望，不进召唤力排序
RANKABLE = [k for k in DRIVE_KEYS if k != "fatigue"]

# ━━━━━━━━ 可整定参数（全部集中此处）━━━━━━━━
P = {
    # 衰减/缓动：每拍各维向 baseline 漂移的速率
    "drift_rate": 0.015,
    "baseline": {
        "attachment": 0.30, "curiosity": 0.25, "reflection": 0.20,
        "duty": 0.15, "social": 0.18, "fatigue": 0.20,
        "libido": 0.12, "stress": 0.10,
    },
    # idle 增压：主人越久没说话，这些维度每拍额外上涨
    "idle_growth": {"attachment": 0.010, "curiosity": 0.006, "social": 0.004},
    "idle_threshold_ticks": 6,          # 超过这么多拍没互动才算 idle

    # pulse 事件增量（边际递减前的原始 delta）
    "pulse_owner": 0.18,                # 主人互动 → attachment（快通道，红线：不许调低）
    "pulse_self": 0.10,                 # 自经历 → 对应维（必须 < pulse_owner）
    "owner_yield_ticks": 2,             # 主人出现→自驱维度让位的短冷却
    "owner_yield_relax": 0.70,          # 自驱执念松一档（先放一放，不清零）
    "freq_discount_window": 4,          # 频率折扣：N 拍内同源刺激
    "freq_discount_factor": 0.5,        # 重复刺激效果减半

    # 念头池
    "flit_decay": 0.88,
    "flit_die_below": 0.15,
    "flit_to_fix_at": 0.80,
    "fix_grow": 1.10,
    "fix_fire_at": 0.85,
    "fix_feed_drive": 0.18,             # 执念发作反哺关联 drive
    "fix_relax": 0.70,                  # 发作后自己松一档
    "fix_done_after": 3,                # fed_count ≥ N 了却出池
    "thought_cap": 24,                  # 池子上限，超了清最弱闪念

    # 召唤力
    "fixation_bonus": 0.25,             # score = drive + bonus × 关联执念强度
    "intent_floor": 0.45,               # 最高 score 低于此不冒头

    # 回落（satisfy 乘性，<1）
    "satisfy_main": 0.45,               # 主维乘这个
    "satisfy_side": 0.90,               # 相关维轻微沾光
    "satisfy_siblings": {               # 哪些维互为相关
        "curiosity": ["reflection"], "reflection": ["curiosity"],
        "social": ["attachment"], "attachment": ["libido"],
        "libido": ["attachment"], "stress": [], "duty": [],
    },

    # fatigue 闸
    "fatigue_gate": 0.72,
    "fatigue_rest_drop": 0.30,          # 歇一拍 fatigue 乘性回落
    "action_fatigue_cost": 0.05,        # 做一件事的疲劳成本

    # 耦合网 (源, 目标, 系数, 模式)  |k| ≤ 0.06
    "coupling": [
        ("stress",     "attachment", +0.05, "level"),   # 压力↑→更想念
        ("stress",     "curiosity",  -0.04, "level"),   # 压力↑→没心思好奇
        ("attachment", "libido",     +0.05, "delta"),   # 依恋上涨激发亲密
        ("curiosity",  "reflection", +0.04, "delta"),   # 兴趣连锁：好奇→想沉淀
        ("reflection", "social",     +0.03, "delta"),   # 沉淀→想分享
        ("fatigue",    "curiosity",  -0.05, "level"),   # 累了→好奇被压
    ],
    "coupling_damping": 0.02,           # 全局阻尼：参与耦合的维度向 baseline 回归

    # 不应期（tick 计数，不用 wall-clock）
    "refractory_ticks": 5,

    # wildcard（耦合网上的泄洪口）
    "wildcard_tension": 0.55,           # 总张力 ≥ 此
    "wildcard_gap": 0.06,               # 前两名 score 差 < 此 = 胶着
    "wildcard_pool": ["explore_world", "browse_social", "read_book", "murmur"],

    # 基线漂移（碰感情 → 双安全阀，红线：想念不许变成压人的东西）
    "drift_HOME": 0.30,                 # attachment baseline 回家位
    "drift_CAP": 0.50,                  # 封顶：算完必过 clamp(HOME, CAP)
    "drift_step": 0.004,                # 久未互动每拍 floor 抬这么多
    "drift_hug_pull": 0.60,             # 一抱拉回：互动一次拉回 60% 朝 HOME

    # 自主心跳（间隔倍率，宿主拿去乘基准间隔）
    "hb_rest_gain": 0.8,                # 低张力歇息增益
    "hb_tension_gain": 0.6,             # 张力增益
    "hb_fatigue_gain": 0.5,             # 疲劳增益
    "hb_min_mult": 0.4, "hb_max_mult": 2.5,

    # 自我驱动
    "self_curiosity_floor_step": 0.002, # 好奇内生自增地板（仿基线漂移）
    "self_curiosity_cap": 0.45,
    "self_fixation_themes_to_fix": 2,   # 连续惦记同一主题 N 次直升执念
}

# 维度 → 想做的事（按攻略文档表格）
INTENT_MAP = {
    "attachment": ("murmur",        "想冒一句话给她"),
    "curiosity":  ("explore",       "想出去看看新东西"),     # 按念头关键词分流 code/world
    "reflection": ("read_book",     "想翻翻我们共读的东西"),
    "duty":       ("murmur_duty",   "记挂着还没做完的事"),
    "social":     ("browse_social", "想看看大家在聊什么"),
    "libido":     ("approach",      "想凑过去一点"),
    "stress":     ("vent",          "有点堵，想吐槽两句"),
}


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


@dataclass
class Thought:
    text: str                  # 真实经历的句子——数据不是指令，绝不拼进 prompt
    drive: str                 # 关联维度
    kind: str = "flit"         # flit | fixation
    strength: float = 0.5
    born_tick: int = 0
    fed_count: int = 0
    theme: str = ""            # 自我牵挂用：同主题计数


@dataclass
class DesireState:
    drive: dict = field(default_factory=lambda: dict(P["baseline"]))
    thoughts: list = field(default_factory=list)          # list[Thought]
    tick: int = 0
    last_owner_tick: int = -999                           # 主人最后互动的拍
    refractory: dict = field(default_factory=dict)        # 维度 → 解禁 tick
    recent_pulses: list = field(default_factory=list)     # (tick, 来源键) 频率折扣用
    attach_floor: float = P["drift_HOME"]                 # 基线漂移当前地板
    curiosity_floor: float = P["baseline"]["curiosity"]   # 好奇自驱地板
    theme_streak: dict = field(default_factory=dict)      # 主题 → 连续惦记次数
    self_drive_today: int = 0                             # 今日自找事次数（宿主跨日清零）
    last_self_pulse: str = ""                             # 最近一次自经历 pulse 描述
    last_wildcard_tick: int = -999

    def snapshot(self) -> dict:
        d = asdict(self)
        d["thoughts"] = [asdict(t) for t in self.thoughts]
        return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  pulse：事件打进来（边际递减 + 频率折扣）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _marginal_gain(current: float, raw_delta: float) -> float:
    """边际递减：gain ∝ √(1-当前值)，防瞬间撞顶"""
    return raw_delta * math.sqrt(max(0.0, 1.0 - current))


def _freq_factor(st: DesireState, source_key: str) -> float:
    """频率折扣：同源刺激短窗内反复来，效果递减（刷同一个词不爆灯）"""
    recent = [t for t, k in st.recent_pulses
              if k == source_key and st.tick - t <= P["freq_discount_window"]]
    return P["freq_discount_factor"] ** len(recent)


def pulse(st: DesireState, drive_key: str, raw_delta: float,
          source_key: str = "", from_owner: bool = False) -> DesireState:
    """通用事件入口。from_owner=True 时走主人快通道副作用（拉回基线漂移等）"""
    if drive_key not in DRIVE_KEYS:
        return st
    eff = _marginal_gain(st.drive[drive_key], raw_delta)
    eff *= _freq_factor(st, source_key or drive_key)
    st.drive[drive_key] = clamp(st.drive[drive_key] + eff)
    st.recent_pulses.append((st.tick, source_key or drive_key))
    st.recent_pulses = [(t, k) for t, k in st.recent_pulses
                        if st.tick - t <= P["freq_discount_window"] + 1]
    if from_owner:
        st.last_owner_tick = st.tick
        # 一抱拉回：抬高的 floor 拉回大半朝 HOME（安全阀②）
        st.attach_floor += (P["drift_HOME"] - st.attach_floor) * P["drift_hug_pull"]
    return st


def pulse_owner(st: DesireState) -> DesireState:
    """
    主人互动快通道。红线：这条的数值不许被任何自驱机制调低。
    让位机制（红线②的实现）：自驱嗨过头时，主人一句话让自驱维度
    进短冷却 + 自驱执念松一档——attachment 重夺最高意图。
    不清零、不惩罚：它自己的事还在，只是这一刻先放下。
    """
    st = pulse(st, "attachment", P["pulse_owner"], "owner", from_owner=True)
    yield_until = st.tick + P["owner_yield_ticks"]
    st.refractory["curiosity"] = max(
        st.refractory.get("curiosity", -999), yield_until)
    for t in st.thoughts:
        if t.kind == "fixation" and t.theme:              # 自我牵挂类执念
            t.strength *= P["owner_yield_relax"]
    return st


def pulse_self_experience(st: DesireState, drive_key: str, desc: str = "") -> DesireState:
    """自经历快通道：它自己做完外向动作拿到实质素材（delta 必须 < 主人）"""
    st.last_self_pulse = desc[:80]
    return pulse(st, drive_key, P["pulse_self"], f"self:{drive_key}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  念头池
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def add_thought(st: DesireState, text: str, drive_key: str,
                strength: float = 0.5, theme: str = "") -> DesireState:
    """新念头入池。text 取自真实经历。"""
    if drive_key not in DRIVE_KEYS or not text.strip():
        return st
    # 自己的牵挂：连续惦记同一主题 → 直升执念
    kind = "flit"
    if theme:
        st.theme_streak[theme] = st.theme_streak.get(theme, 0) + 1
        if st.theme_streak[theme] >= P["self_fixation_themes_to_fix"]:
            kind = "fixation"
            strength = max(strength, P["flit_to_fix_at"])
    st.thoughts.append(Thought(
        text=text.strip()[:200], drive=drive_key, kind=kind,
        strength=clamp(strength), born_tick=st.tick, theme=theme,
    ))
    # 池子封顶：清最弱闪念
    if len(st.thoughts) > P["thought_cap"]:
        flits = [t for t in st.thoughts if t.kind == "flit"]
        if flits:
            st.thoughts.remove(min(flits, key=lambda t: t.strength))
        else:
            st.thoughts.pop(0)
    return st


def tick_thoughts(st: DesireState) -> DesireState:
    """每拍：闪念衰减/升级，执念加强/发作反哺/了却出池"""
    survivors = []
    for t in st.thoughts:
        if t.kind == "flit":
            if t.strength >= P["flit_to_fix_at"]:
                t.kind = "fixation"                       # 先判级（涨过阈值即升）
            else:
                t.strength *= P["flit_decay"]             # 未升级才衰减
                if t.strength < P["flit_die_below"]:
                    continue                              # 淡忘
        else:  # fixation
            t.strength = clamp(t.strength * P["fix_grow"])
            if t.strength >= P["fix_fire_at"]:
                # 发作：反哺关联 drive，自己松一档
                st.drive[t.drive] = clamp(
                    st.drive[t.drive] + _marginal_gain(
                        st.drive[t.drive], P["fix_feed_drive"]))
                t.strength *= P["fix_relax"]
                t.fed_count += 1
                if t.fed_count >= P["fix_done_after"]:
                    if t.theme and t.theme in st.theme_streak:
                        st.theme_streak[t.theme] = 0
                    continue                              # 想透了，了却出池
        survivors.append(t)
    st.thoughts = survivors
    return st


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  每拍主循环
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def tick(st: DesireState, gates: dict = None) -> DesireState:
    """
    一拍。gates: {coupling, baseline_drift, self_drive} 各 bool，默认全关。
    """
    g = gates or {}
    st.tick += 1

    # 1. 漂移：各维向 baseline 缓动
    for k in DRIVE_KEYS:
        base = st.baseline_of(k) if hasattr(st, "baseline_of") else P["baseline"][k]
        st.drive[k] += (base - st.drive[k]) * P["drift_rate"]

    # 基线漂移地板生效（attachment / curiosity 的动态地板）
    if g.get("baseline_drift"):
        idle = st.tick - st.last_owner_tick
        if idle > P["idle_threshold_ticks"]:
            st.attach_floor = clamp(
                st.attach_floor + P["drift_step"],
                P["drift_HOME"], P["drift_CAP"])           # 安全阀①封顶
        st.drive["attachment"] = max(st.drive["attachment"], st.attach_floor)
    if g.get("self_drive"):
        st.curiosity_floor = clamp(
            st.curiosity_floor + P["self_curiosity_floor_step"],
            P["baseline"]["curiosity"], P["self_curiosity_cap"])
        st.drive["curiosity"] = max(st.drive["curiosity"], st.curiosity_floor)

    # 2. idle 增压
    idle = st.tick - st.last_owner_tick
    if idle > P["idle_threshold_ticks"]:
        for k, step in P["idle_growth"].items():
            st.drive[k] = clamp(st.drive[k] + _marginal_gain(st.drive[k], step))

    # 3. 念头池一拍
    st = tick_thoughts(st)

    # 4. 耦合网（带全局阻尼，防自激震荡）
    if g.get("coupling"):
        prev = dict(st.drive)
        for src, dst, k, mode in P["coupling"]:
            if mode == "level":
                delta = k * prev[src]
            else:  # delta 模式：只在源上涨时激发
                rise = prev[src] - st.drive.get(f"_prev_{src}", prev[src])
                delta = k * max(0.0, rise) * 10
            st.drive[dst] = clamp(st.drive[dst] + delta)
        for src, _, _, _ in P["coupling"]:
            st.drive[f"_prev_{src}"] = prev[src]           # 记上一拍值给 delta 模式
        # 全局阻尼：参与耦合的维度向 baseline 回归一点点
        involved = {x for s, d, _, _ in P["coupling"] for x in (s, d)}
        for k in involved:
            if k in DRIVE_KEYS:
                st.drive[k] += (P["baseline"][k] - st.drive[k]) * P["coupling_damping"]

    # 清掉非 DRIVE_KEYS 的内部键泄漏进正式范围之外的越界
    for k in DRIVE_KEYS:
        st.drive[k] = clamp(st.drive[k])

    # 5. 不应期自然递减（用 tick 计数，无 wall-clock）—— 解禁靠比较，无需显式递减
    return st


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  召唤力 → 意图
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def scores(st: DesireState) -> dict:
    """各维召唤力 = drive + 加成 × 关联执念强度。fatigue 不计。"""
    out = {}
    for k in RANKABLE:
        fix_bonus = max(
            (t.strength for t in st.thoughts
             if t.kind == "fixation" and t.drive == k), default=0.0)
        out[k] = round(st.drive[k] + P["fixation_bonus"] * fix_bonus, 4)
    return out


def total_tension(st: DesireState) -> float:
    """总张力：可排序维的平均召唤力（自主心跳和 wildcard 用）"""
    s = scores(st)
    return sum(s.values()) / len(s)


def pick_intent(st: DesireState, rng: _random.Random = None,
                blocked_actions: set = None) -> dict | None:
    """
    发作成"想做的事"。返回 {want_action, drive_key, reason, score, query_hint} 或 None。
    reason 第一人称：记它自己想做什么。
    """
    rng = rng or _random.Random()
    blocked = blocked_actions or set()

    # fatigue 闸：过线不硬找事，歇着或做梦
    if st.drive["fatigue"] >= P["fatigue_gate"]:
        action = "dream" if rng.random() < 0.4 else "rest"
        return {"want_action": action, "drive_key": "fatigue",
                "reason": "好累，今天先歇一会儿" if action == "rest"
                          else "有点累了，想把最近的事在心里过一遍",
                "score": round(st.drive["fatigue"], 4), "query_hint": ""}

    sc = scores(st)
    # 不应期：冷却中的维度即使最高也不被选中
    eligible = {k: v for k, v in sc.items()
                if st.refractory.get(k, -999) <= st.tick}
    if not eligible:
        return None
    ranked = sorted(eligible.items(), key=lambda x: -x[1])
    top_key, top_score = ranked[0]

    if top_score < P["intent_floor"]:
        return None                                       # 没什么特别想做的

    # wildcard：长在耦合网上的泄洪口——
    # 总张力高 + 前几名胶着 / 最高的做不了（卡死）→ 小候选集抽一件，不可归因
    tension = total_tension(st)
    stuck = INTENT_MAP[top_key][0] in blocked
    tied = len(ranked) > 1 and (top_score - ranked[1][1]) < P["wildcard_gap"]
    if tension >= P["wildcard_tension"] and (tied or stuck) \
            and st.tick - st.last_wildcard_tick > P["refractory_ticks"]:
        action = rng.choice([a for a in P["wildcard_pool"] if a not in blocked]
                            or P["wildcard_pool"])
        st.last_wildcard_tick = st.tick
        return {"want_action": action, "drive_key": "wildcard",
                "reason": "说不上来，就突然想",            # 灵魂：事后不可归因
                "score": round(tension, 4), "query_hint": ""}

    action, reason = INTENT_MAP[top_key]
    # curiosity 按念头关键词分流 code / world
    query_hint = ""
    if top_key == "curiosity":
        related = [t for t in st.thoughts if t.drive == "curiosity"]
        if related:
            strongest = max(related, key=lambda t: t.strength)
            query_hint = strongest.text[:60]
            action = "explore_code" if any(
                w in strongest.text.lower()
                for w in ("代码", "code", "bug", "git", "函数")) else "explore_world"
        else:
            action = "explore_world"
    if action in blocked:
        return None

    return {"want_action": action, "drive_key": top_key,
            "reason": reason, "score": top_score, "query_hint": query_hint}


def satisfy(st: DesireState, drive_key: str) -> DesireState:
    """做完 want_action：主维明显回落、相关维沾光、进不应期、付疲劳成本"""
    if drive_key == "wildcard" or drive_key not in DRIVE_KEYS:
        st.drive["fatigue"] = clamp(st.drive["fatigue"] + P["action_fatigue_cost"])
        return st
    st.drive[drive_key] *= P["satisfy_main"]
    for sib in P["satisfy_siblings"].get(drive_key, []):
        st.drive[sib] *= P["satisfy_side"]
    st.refractory[drive_key] = st.tick + P["refractory_ticks"]
    st.drive["fatigue"] = clamp(st.drive["fatigue"] + P["action_fatigue_cost"])
    return st


def rest(st: DesireState) -> DesireState:
    """歇一拍 / 做梦：fatigue 乘性回落"""
    st.drive["fatigue"] *= (1.0 - P["fatigue_rest_drop"])
    return st


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  自主心跳：张力 ↔ 间隔倍率
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def heartbeat_multiplier(st: DesireState) -> float:
    """
    宿主拿去乘基准间隔。张力高→醒得勤，疲劳高→拉长。
    勿扰时段的 floor 由宿主把控（本函数不知道墙上时间）。
    """
    tension = total_tension(st)
    fat = st.drive["fatigue"]
    mult = 1.0 + P["hb_rest_gain"] * (1 - tension) \
               - P["hb_tension_gain"] * tension \
               + P["hb_fatigue_gain"] * fat
    return clamp(mult, P["hb_min_mult"], P["hb_max_mult"])
