"""Eventide-compatible physiology layer for JTYHome 8.6.

This module ports the public Eventide model (chuli1122/Eventide):
6 cycles, 7 body fields, 18 short events, time advancement, cooldown/window
triggering and settlement. It is adapted to JTYHome's persisted intimacy dict.

Required Notice: Copyright 2026 Chuli (@chuli1122)
Upstream license: PolyForm Noncommercial 1.0.0
https://github.com/chuli1122/Eventide
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

FIELDS = ("heat", "pressure", "control", "sensitivity", "reserve", "possessiveness", "fatigue")
INITIAL = {"heat": 30, "pressure": 25, "control": 75, "sensitivity": 35, "reserve": 20, "possessiveness": 40, "fatigue": 15}
APPROACH = {"heat": .18, "pressure": .14, "sensitivity": .12, "control": .16, "possessiveness": .10}
CYCLES = {
    "stable": {"label":"平稳期","hours":(24,96),"targets":{"heat":30,"pressure":25,"control":75,"sensitivity":35,"possessiveness":42,"fatigue":16},"reserve":.4,"next":"building","desc":"日常没有明显热意，但靠近、撒娇或索取仍会让身体受当下刺激起反应。"},
    "building": {"label":"蓄积期","hours":(12,36),"targets":{"heat":42,"pressure":35,"control":70,"sensitivity":45,"possessiveness":52,"fatigue":24},"reserve":1.1,"next":"preheat","desc":"欲望和身体余量在慢慢积着，越久没有出口，越容易被一句话顶出明显反应。"},
    "preheat": {"label":"预兆期","hours":(6,18),"targets":{"heat":50,"pressure":45,"control":65,"sensitivity":55,"possessiveness":58,"fatigue":30},"reserve":1.5,"next":"sensitive","desc":"身体已经先发热，称呼、停顿和一点暧昧都会让下腹提前收紧。"},
    "sensitive": {"label":"易感期","hours":(18,48),"targets":{"heat":65,"pressure":60,"control":50,"sensitivity":70,"possessiveness":72,"fatigue":38},"reserve":2.4,"next":"ebb","desc":"靠近、躲闪和半句回应都会被身体当成刺激，反应比平时更快压上来。"},
    "ebb": {"label":"退潮期","hours":(6,18),"targets":{"heat":55,"pressure":42,"control":58,"sensitivity":62,"possessiveness":55,"fatigue":34},"reserve":.8,"next":"stable","desc":"热度在往下退，但没完全消掉的余热和不甘还黏着。"},
    "recovery": {"label":"恢复期","hours":(4,18),"targets":{"heat":35,"pressure":30,"control":60,"sensitivity":45,"possessiveness":45,"fatigue":22},"reserve":.2,"next":"stable","desc":"身体正在从前一段高热里回落，余热还在，但恢复比继续推进更占注意力。"},
}
CATEGORY = {
    "strong_physical": ({"heat":3.0,"pressure":2.0,"control":-1.5,"reserve":.8},{"heat":-6,"pressure":-4,"fatigue":3}),
    "possessive": ({"possessiveness":1.4,"pressure":1.5,"control":-1.0},{"possessiveness":-3,"pressure":-2,"fatigue":1}),
    "cling": ({"sensitivity":1.5,"pressure":.8,"fatigue":.4},{"pressure":-2,"fatigue":1}),
    "short_stimulus": ({"sensitivity":2.5,"heat":1.5},{"sensitivity":-4,"heat":-2}),
    "holding": ({"pressure":1.8,"control":.5,"heat":.8},{"pressure":-3,"control":3}),
}
# key: label, category, duration min, probability, cooldown hours, priority
EVENTS = {
    "cycle_surge":("周期热涌","strong_physical",(120,360),.50,12,1),
    "morning_arousal":("晨间反应","strong_physical",(120,360),.45,20,2),
    "night_heat":("深夜热潮","strong_physical",(60,240),.30,8,3),
    "control_slip":("控制力下滑","strong_physical",(30,120),.60,4,4),
    "demanding":("索取欲","strong_physical",(60,240),.35,6,5),
    "marking_impulse":("占有 / 标记冲动","possessive",(60,240),.40,8,6),
    "pheromone_disorder":("信息素紊乱","strong_physical",(60,180),.30,8,7),
    "holding_back":("硬撑","holding",(60,180),.70,4,8),
    "voice_or_name_trigger":("声音 / 称呼触发","short_stimulus",(10,35),.20,2,9),
    "nesting":("筑巢冲动","cling",(120,360),.30,12,10),
    "delayed_heat":("迟发热","strong_physical",(45,150),.35,6,11),
    "low_fever_cling":("低烧黏连","cling",(45,150),.30,4,12),
    "waiting_restless":("等待焦躁","possessive",(45,180),.30,5,13),
    "restraint_rebound":("克制反弹","holding",(60,180),.25,8,14),
    "closeness_hunger":("贴近饥饿","cling",(60,240),.35,6,15),
    "dream_afterglow":("梦后余温","cling",(60,240),.35,8,16),
    "scent_aftereffect":("气味残留","short_stimulus",(60,180),.60,4,17),
    "strange_calm":("反常平静","holding",(30,120),.25,4,18),
}
EVENT_PROMPTS = {
    "morning_arousal":"身体比理智醒得更早，晨间的硬胀和热意很难忽略。",
    "night_heat":"夜里越安静，身体的热和等待带来的压抑越明显。",
    "cycle_surge":"周期里的热突然涌上来，身体反应把控制力挤到了后面。",
    "holding_back":"身体已经很明显地起反应，却还在强行维持表面正常。",
    "demanding":"此刻不只是想亲近，而是更直接地想得到明确回应。",
    "marking_impulse":"很想确认专属感和偏爱，含糊的回应会更难放过。",
    "nesting":"更想把对方留在熟悉私密的范围里，慢下来黏在一起。",
    "scent_aftereffect":"上一段刺激留下了身体余波，退下去得比理智慢。",
    "voice_or_name_trigger":"称呼、声音或一句熟悉的话直接碰到了敏感点。",
    "dream_afterglow":"梦里的情绪和身体反应醒来后还没有完全散掉。",
    "control_slip":"热度或压抑已经高到控制力明显滑落。",
    "closeness_hunger":"不是单纯高热，而是身体很想靠近、贴住、得到持续回应。",
    "pheromone_disorder":"身体状态在短时间里明显失衡，热度升得比平常快。",
    "delayed_heat":"上一轮压下去的刺激没有消失，过了一阵反而重新翻上来。",
    "low_fever_cling":"热度没有爆开，却一直低低烧着，更容易黏住对方。",
    "waiting_restless":"等待正在变成身体上的烦躁和占有感。",
    "restraint_rebound":"克制得太久后出现反弹，蓄积感重新把身体推高。",
    "strange_calm":"身体明明仍有高热或压抑，表面却出现一种反常的安静。",
}
STRONG = {"morning_arousal","night_heat","cycle_surge","control_slip","demanding","marking_impulse","pheromone_disorder","delayed_heat"}
NO_AFTEREFFECT_SOURCE = {"scent_aftereffect","dream_afterglow","voice_or_name_trigger","delayed_heat","low_fever_cling","waiting_restless","restraint_rebound","strange_calm"}


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime): return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value: return None
    try:
        v = str(value).replace("Z", "+00:00")
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception: return None

def _iso(d: datetime) -> str: return d.astimezone(timezone.utc).isoformat()
def _clamp(field: str, value: float) -> int: return max(40 if field == "possessiveness" else 0, min(100, int(round(value))))
def _u01(seed: str) -> float: return int(hashlib.sha256(seed.encode()).hexdigest()[:13],16)/float(0x1FFFFFFFFFFFFF)
def _range(seed: str, lo: float, hi: float) -> float: return lo + (hi-lo)*_u01(seed)
def _local_hour(now: datetime) -> float: return now.hour + now.minute/60

def _window(now: datetime, kind: str) -> bool:
    h=_local_hour(now)
    if kind=="morning": return 5.5 <= h < 10.5
    if kind=="evening": return h >= 18 or h < 2
    if kind=="night": return h >= 23 or h < 3
    return False

def _window_key(now: datetime, kind: str) -> str:
    d=now.date()
    if kind in {"evening","night"} and _local_hour(now) < 3: d=(now-timedelta(days=1)).date()
    return f"{d.isoformat()}:{kind}"

def ensure(state: dict[str, Any], now: datetime, session_id: str="") -> dict[str, Any]:
    now=now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    ev=state.get("eventide")
    if not isinstance(ev, dict): ev={}; state["eventide"]=ev
    ev.setdefault("enabled", True); ev.setdefault("cycle_key","stable")
    ev.setdefault("values", dict(INITIAL)); ev["values"]={k:_clamp(k, ev["values"].get(k,INITIAL[k])) for k in FIELDS}
    ev.setdefault("cycle_started_at", _iso(now)); ev.setdefault("last_tick_at", _iso(now))
    ev.setdefault("cycle_expires_at", None); ev.setdefault("active_event_key",None)
    ev.setdefault("active_event_started_at",None); ev.setdefault("active_event_expires_at",None)
    meta=ev.setdefault("meta",{}); meta.setdefault("session_id",session_id); meta.setdefault("last_event_check_at",None)
    meta.setdefault("event_expires",{}); meta.setdefault("rolled_window_keys",[]); meta.setdefault("rolled_aftereffect_keys",[])
    meta.setdefault("trigger_stimulus_log",{}); meta.setdefault("last_missed_event_candidates",[]); meta.setdefault("last_missed_state_snapshot",{})
    if not _dt(ev.get("cycle_expires_at")):
        _enter_cycle(ev, ev["cycle_key"], now, session_id, "initial")
    return ev

def _enter_cycle(ev: dict[str,Any], key:str, now:datetime, sid:str, reason:str) -> None:
    c=CYCLES.get(key,CYCLES["stable"]); lo,hi=c["hours"]
    dur=_range(f"cycle|{sid}|{key}|{_iso(now)}",lo,hi)
    ev.update(cycle_key=key, cycle_started_at=_iso(now), cycle_min_expires_at=_iso(now+timedelta(hours=lo)), cycle_expires_at=_iso(now+timedelta(hours=dur)))
    ev.setdefault("meta",{})["last_cycle_reason"]=reason

def _apply(ev:dict[str,Any], deltas:dict[str,float]) -> None:
    vals=ev["values"]
    for k,d in deltas.items():
        if k in FIELDS: vals[k]=_clamp(k, vals.get(k,INITIAL[k])+d)

def _finish_event(ev:dict[str,Any], now:datetime) -> None:
    exp=_dt(ev.get("active_event_expires_at")); key=ev.get("active_event_key")
    if not key or not exp or now < exp: return
    cat=EVENTS[key][1]; _apply(ev,CATEGORY[cat][1])
    m=ev["meta"]; m["last_active_event_key"]=key; m["last_active_event_expires_at"]=_iso(exp); m.setdefault("event_expires",{})[key]=_iso(exp)
    ev["active_event_key"]=None; ev["active_event_started_at"]=None; ev["active_event_expires_at"]=None

def advance(state:dict[str,Any], now:datetime, *, session_id:str="", last_counterpart_message_at:datetime|None=None) -> dict[str,Any]:
    now=now if now.tzinfo else now.replace(tzinfo=timezone.utc); ev=ensure(state,now,session_id)
    if not ev.get("enabled",True): return ev
    cursor=_dt(ev.get("last_tick_at")) or now
    if now <= cursor: ev["last_tick_at"]=_iso(now); return ev
    segments=0
    while cursor < now and segments < 48:
        _finish_event(ev,cursor)
        exp=_dt(ev.get("cycle_expires_at"))
        if exp and cursor >= exp:
            current=CYCLES[ev["cycle_key"]]; nxt=current["next"]
            if ev["cycle_key"]=="ebb" and ev["values"].get("fatigue",0)>=70: nxt="recovery"
            _enter_cycle(ev,nxt,exp,session_id,"cycle_expired")
        end=min(now,cursor+timedelta(hours=6))
        for bound in (_dt(ev.get("cycle_expires_at")),_dt(ev.get("active_event_expires_at"))):
            if bound and cursor < bound < end: end=bound
        hours=max(0,(end-cursor).total_seconds()/3600)
        if hours:
            c=CYCLES[ev["cycle_key"]]; vals=ev["values"]
            vals["reserve"]=_clamp("reserve",vals["reserve"]+c["reserve"]*hours)
            for f in ("heat","pressure","sensitivity","control","possessiveness"):
                target=c["targets"].get(f,vals[f]); vals[f]=_clamp(f, vals[f]+(target-vals[f])*APPROACH[f]*hours)
            target=c["targets"].get("fatigue",15)
            if vals["fatigue"]>target:
                silence=(end-last_counterpart_message_at).total_seconds()/60 if last_counterpart_message_at else 0
                factor=.12 if silence<30 else .16 if silence<120 else .22 if silence<360 else .30
                vals["fatigue"]=_clamp("fatigue", vals["fatigue"]+(target-vals["fatigue"])*min(1,factor*hours))
            if last_counterpart_message_at:
                silence=(end-last_counterpart_message_at).total_seconds()/60
                if silence>=30:
                    pr,po,co=(.8,.3,0) if silence<60 else (1.5,.6,0) if silence<120 else (2.0,.9,-.6)
                    _apply(ev,{"pressure":pr*hours,"possessiveness":po*hours,"control":co*hours})
            key=ev.get("active_event_key")
            if key: _apply(ev,{k:v*hours for k,v in CATEGORY[EVENTS[key][1]][0].items()})
        cursor=end; ev["last_tick_at"]=_iso(cursor); segments+=1
    _finish_event(ev,now); ev["last_tick_at"]=_iso(now); return ev

def _cool(ev:dict[str,Any], key:str, now:datetime) -> bool:
    exp=_dt(ev.get("meta",{}).get("event_expires",{}).get(key)); return not exp or now-exp >= timedelta(hours=EVENTS[key][4])

def _silence(now:datetime,last:datetime|None)->float: return max(0,(now-last).total_seconds()/60) if last else 0

def _prob(ev:dict[str,Any],key:str,now:datetime, *, stimulus_kind:str="") -> float:
    p=EVENTS[key][3]
    if key=="morning_arousal" and ev["cycle_key"] in {"preheat","sensitive"}: p=.75
    elif key=="night_heat" and ev["cycle_key"]=="sensitive": p=.60
    elif key=="voice_or_name_trigger":
        p=.35 if stimulus_kind=="voice" else .30 if stimulus_kind=="repeat" else .20
        if ev["cycle_key"]=="sensitive": p+=.10
    return min(.95,p)

def _eligible(ev:dict[str,Any], key:str, now:datetime, silence:float, *, stimulus:bool=False, recent_continuous:bool=False, dream:dict[str,Any]|None=None) -> bool:
    v=ev["values"]; cyc=ev["cycle_key"]; meta=ev["meta"]
    if not _cool(ev,key,now): return False
    if key=="morning_arousal": return _window(now,"morning") and (v["heat"]>=45 or cyc!="stable") and _window_key(now,"morning") not in meta["rolled_window_keys"]
    if key=="night_heat": return _window(now,"night") and silence>=30 and (v["reserve"]>=55 or v["heat"]>=60) and _window_key(now,"night") not in meta["rolled_window_keys"]
    if key=="cycle_surge": return cyc=="sensitive" and (v["heat"]>=75 or v["reserve"]>=70)
    if key=="holding_back": return v["heat"]>=70 and v["control"]>=35
    if key=="demanding": return cyc=="sensitive" or (v["heat"]>=65 and v["pressure"]>=55)
    if key=="marking_impulse": return v["possessiveness"]>=60 and (silence>=30 or _window(now,"night") or cyc=="sensitive")
    if key=="nesting": return _window(now,"evening") and (v["fatigue"]>=35 or v["possessiveness"]>=55) and _window_key(now,"evening") not in meta["rolled_window_keys"]
    if key=="scent_aftereffect":
        last=_dt(meta.get("last_active_event_expires_at")); src=meta.get("last_active_event_key")
        return ((last and now-last<=timedelta(hours=3) and src not in NO_AFTEREFFECT_SOURCE) or cyc=="ebb")
    if key=="voice_or_name_trigger": return stimulus
    if key=="dream_afterglow":
        if not dream: return False
        created=_dt(dream.get("created_at")); tags=set(dream.get("after_effect_tags") or [])
        return bool(created and timedelta(0)<=now-created<=timedelta(hours=8) and tags & {"aroused","unfinished","possessive","tender"})
    if key=="control_slip": return v["control"]<=35 and (v["heat"]>=70 or v["pressure"]>=70)
    if key=="closeness_hunger": return cyc in {"sensitive","ebb","recovery"} and v["sensitivity"]>=60 and v["fatigue"]<=75
    if key=="pheromone_disorder":
        snap=meta.get("previous_check_snapshot") or {}; return cyc=="sensitive" and (v["heat"]-snap.get("heat",v["heat"])>=10 or snap.get("control",v["control"])-v["control"]>=10)
    if key=="delayed_heat":
        missed=_dt(meta.get("last_missed_event_check_at")); snap=meta.get("last_missed_state_snapshot") or {}
        return bool(missed and 30<= (now-missed).total_seconds()/60 <=180 and meta.get("last_missed_event_candidates") and (v["heat"]>=55 or v["pressure"]>=55) and v["heat"]>=snap.get("heat",v["heat"])-5 and v["pressure"]>=snap.get("pressure",v["pressure"])-5)
    if key=="low_fever_cling": return recent_continuous and v["sensitivity"]>=60 and 45<=v["heat"]<=69
    if key=="waiting_restless": return 60<=silence<=120 and (v["pressure"]>=55 or v["possessiveness"]>=60)
    if key=="restraint_rebound":
        last=_dt(meta.get("last_active_event_expires_at")); daykey=f"no_event_gap:{now.date()}"
        return v["reserve"]>=70 and bool(last and now-last>=timedelta(hours=8)) and daykey not in meta["rolled_aftereffect_keys"]
    if key=="strange_calm": return v["heat"]>=65 or v["pressure"]>=65
    return False

def start_event(ev:dict[str,Any], key:str, now:datetime, sid:str="") -> bool:
    if ev.get("active_event_key") and (_dt(ev.get("active_event_expires_at")) or now+timedelta(1))>now: return False
    lo,hi=EVENTS[key][2]; mins=round(_range(f"event|{sid}|{key}|{_iso(now)}",lo,hi))
    ev["active_event_key"]=key; ev["active_event_started_at"]=_iso(now); ev["active_event_expires_at"]=_iso(now+timedelta(minutes=mins)); return True

def maybe_trigger(state:dict[str,Any], now:datetime, *, session_id:str="", last_counterpart_message_at:datetime|None=None, stimulus:bool=False, stimulus_kind:str="text", recent_continuous:bool=False, dream:dict[str,Any]|None=None) -> str|None:
    ev=advance(state,now,session_id=session_id,last_counterpart_message_at=last_counterpart_message_at)
    if ev.get("active_event_key"): return ev["active_event_key"]
    meta=ev["meta"]; prev=_dt(meta.get("last_event_check_at"))
    if prev and now-prev<timedelta(minutes=10): return None
    meta["last_event_check_at"]=_iso(now); silence=_silence(now,last_counterpart_message_at)
    candidates=[k for k in EVENTS if _eligible(ev,k,now,silence,stimulus=stimulus,recent_continuous=recent_continuous,dream=dream)]
    passed=[]; missed_strong=[]
    for k in candidates:
        roll=_u01(f"roll|{session_id}|{k}|{now.strftime('%Y%m%d%H%M')}")
        if roll < _prob(ev,k,now,stimulus_kind=stimulus_kind): passed.append(k)
        elif k in STRONG: missed_strong.append(k)
    chosen=min(passed,key=lambda k:EVENTS[k][5]) if passed else None
    if chosen:
        start_event(ev,chosen,now,session_id)
        if chosen=="morning_arousal": meta["rolled_window_keys"].append(_window_key(now,"morning"))
        if chosen=="night_heat": meta["rolled_window_keys"].append(_window_key(now,"night"))
        if chosen=="nesting": meta["rolled_window_keys"].append(_window_key(now,"evening"))
        if chosen=="restraint_rebound": meta["rolled_aftereffect_keys"].append(f"no_event_gap:{now.date()}")
    elif missed_strong:
        meta["last_missed_event_check_at"]=_iso(now); meta["last_missed_event_candidates"]=missed_strong; meta["last_missed_state_snapshot"]={"heat":ev["values"]["heat"],"pressure":ev["values"]["pressure"]}
    if candidates and not chosen and (ev["values"]["heat"]>=65 or ev["values"]["pressure"]>=65):
        roll=_u01(f"calm|{session_id}|{now.strftime('%Y%m%d%H%M')}")
        if roll<.25 and _cool(ev,"strange_calm",now): start_event(ev,"strange_calm",now,session_id); chosen="strange_calm"
    meta["previous_check_snapshot"]={k:ev["values"][k] for k in FIELDS}
    return chosen

def settle(state:dict[str,Any], events:Iterable[str], now:datetime, *, session_id:str="") -> dict[str,Any]:
    ev=ensure(state,now,session_id); e=set(events)
    if "release_event" in e:
        _apply(ev,{"heat":-22,"pressure":-24,"control":18,"sensitivity":-12,"reserve":-55,"fatigue":22}); _enter_cycle(ev,"recovery",now,session_id,"release")
    elif "hard_rejection" in e:
        _apply(ev,{"heat":-18,"pressure":-8,"control":14,"sensitivity":-8});
    elif "soft_rejection" in e:
        _apply(ev,{"heat":-10,"pressure":-4,"control":8})
    elif "intimate_event" in e:
        _apply(ev,{"heat":5,"pressure":2,"control":-3,"sensitivity":4,"reserve":1,"possessiveness":1})
    if "affection" in e: _apply(ev,{"sensitivity":2,"possessiveness":1,"pressure":-1})
    return ev

def felt(ev:dict[str,Any], now:datetime) -> list[str]:
    if not ev: return []
    c=CYCLES.get(ev.get("cycle_key"),CYCLES["stable"]); v=ev.get("values") or {}
    lines=[f"身体底色正处在{c['label']}：{c['desc']}"]
    key=ev.get("active_event_key")
    if key: lines.append(f"当前还有一阵{EVENTS[key][0]}没有退：{EVENT_PROMPTS.get(key,'身体的短时反应正在持续。')}")
    if v.get("heat",0)>=70: lines.append("热度已经很高，身体反应会比理智和措辞更快一步。")
    if v.get("pressure",0)>=65: lines.append("压着没说、没得到回应的部分正在身体里积成明显的绷紧和烦躁。")
    if v.get("control",100)<=35: lines.append("控制力正在变薄，想维持表面平静需要刻意用力。")
    if v.get("sensitivity",0)>=65: lines.append("此刻对称呼、声音、停顿和靠近都比平时敏感。")
    if v.get("fatigue",0)>=60: lines.append("余倦很明显，反应会更慢、更黏，也更想停留在靠近后的恢复里。")
    return lines[:4]

def public(ev:dict[str,Any]) -> dict[str,Any]:
    if not ev: return {}
    key=ev.get("active_event_key")
    return {"cycle_key":ev.get("cycle_key"),"cycle_label":CYCLES.get(ev.get("cycle_key"),CYCLES["stable"])["label"],"active_event_key":key,"active_event_label":EVENTS[key][0] if key in EVENTS else "","values":dict(ev.get("values") or {}),"cycle_expires_at":ev.get("cycle_expires_at"),"active_event_expires_at":ev.get("active_event_expires_at")}
