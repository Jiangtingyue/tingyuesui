"""Local Ocean Listen audio analysis with an isolated, sequential worker.

Audio never needs to be uploaded to a model provider.  Ocean Listen produces a
bounded evidence report for every model, plus a spectrogram for native vision
lanes.  Heavy ML dependencies live in their own Python 3.10 environment so a
failed model install cannot stop normal chat startup.
"""
from __future__ import annotations

import json
import os
import platform
import re
import signal
import shutil
import subprocess
import threading
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from attachment_service import AttachmentService, attachment_service
from runtime_paths import DATA_DIR, UPLOAD_DIR


SOURCE_ROOT = Path(__file__).resolve().parent
OCEAN_SOURCE_DIR = SOURCE_ROOT / "vendor" / "ocean-listen"
OCEAN_DATA_DIR = DATA_DIR / "ocean-listen"
OCEAN_RUNTIME_DIR = DATA_DIR / "ocean-runtime"
OCEAN_VENV_DIR = OCEAN_RUNTIME_DIR / ".venv"
OCEAN_READY_MARKER = OCEAN_RUNTIME_DIR / "ready.json"
OCEAN_INSTALL_LOG = OCEAN_RUNTIME_DIR / "install.log"
OCEAN_INSTALL_SCRIPT = SOURCE_ROOT / "install_ocean_listen_mac.command"
MAX_SUMMARY_CHARS = 24000
MAX_LOG_BYTES = 512_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mmss(value: Any) -> str:
    try:
        seconds = max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}:{seconds % 60:02d}"


def _number(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _spans(values: Any, limit: int = 8) -> str:
    if not isinstance(values, list):
        return ""
    result: list[str] = []
    for value in values[:limit]:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            result.append(f"{_mmss(value[0])}–{_mmss(value[1])}")
        elif isinstance(value, dict):
            result.append(f"{_mmss(value.get('start'))}–{_mmss(value.get('end'))}")
    if len(values) > limit:
        result.append(f"另 {len(values) - limit} 段")
    return "、".join(result)


def _note_name_from_midi(value: Any) -> str:
    try:
        pitch = int(value)
    except (TypeError, ValueError):
        return "?"
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return f"{names[pitch % 12]}{pitch // 12 - 1}"


def build_ocean_summary(data: dict[str, Any], *, filename: str = "音频") -> str:
    """Compress Ocean's large arrays into a prompt-sized, source-faithful report."""
    if not isinstance(data, dict):
        raise ValueError("听海报告不是有效对象")
    lines = [
        "【听海 · 本机听觉报告】",
        f"文件：{filename}",
        "说明：原音频留在本机；下列内容由 Ocean Listen 从声波测量并压缩。"
        "它是分析材料，不等于审美结论；转写歌词可能听错。",
    ]

    duration = data.get("duration")
    classification = data.get("classification") if isinstance(data.get("classification"), dict) else {}
    audio_type = classification.get("type") or "未分类"
    confidence = classification.get("confidence")
    basics = [
        f"时长 {_mmss(duration)}",
        f"类型 {audio_type}" + (f"（置信度 {_number(float(confidence) * 100, 0)}%）" if confidence is not None else ""),
        f"速度 {_number(data.get('bpm'), 0)} BPM",
        f"主调 {data.get('key') or '?'}",
        f"人声覆盖 {_number(float(data.get('vocalCoverage') or 0) * 100, 0)}%",
        f"打击性比例 {_number(data.get('percussiveRatio'))}",
    ]
    lines.append("概况：" + "；".join(basics) + "。")
    reasoning = str(classification.get("reasoning") or "").strip()
    if reasoning:
        lines.append(f"自动分类依据：{reasoning}")

    segments = data.get("segments") if isinstance(data.get("segments"), list) else []
    if segments:
        energies = [float(item.get("avgEnergy") or 0) for item in segments if isinstance(item, dict)]
        if energies:
            maximum = max(energies) or 1
            bars = "".join("▁▂▃▄▅▆▇█"[min(7, int(value / maximum * 7.99))] for value in energies)
            peak = max((item for item in segments if isinstance(item, dict)), key=lambda item: float(item.get("avgEnergy") or 0))
            quiet = min((item for item in segments if isinstance(item, dict)), key=lambda item: float(item.get("avgEnergy") or 0))
            lines.append(
                f"能量六段：{bars}；最强 {_mmss(peak.get('start'))}–{_mmss(peak.get('end'))}；"
                f"最弱 {_mmss(quiet.get('start'))}–{_mmss(quiet.get('end'))}。"
            )
    chroma = data.get("chromaBySegment")
    if isinstance(chroma, list) and chroma:
        lines.append("调性分段：" + " → ".join(str(item) for item in chroma[:12]))
    brightness = data.get("brightnessBySegment")
    if isinstance(brightness, list) and brightness:
        lines.append(
            "明亮度：趋势 " + str(data.get("brightnessTrend") or "未知")
            + "；六段频谱质心 " + " → ".join(_number(item, 0) for item in brightness[:12]) + " Hz。"
        )

    events = data.get("events") if isinstance(data.get("events"), list) else []
    if events:
        event_text = "；".join(
            f"{_mmss(item.get('t'))} {item.get('label')}"
            for item in events[:24] if isinstance(item, dict)
        )
        lines.append(f"结构事件：{event_text}" + (f"；另 {len(events) - 24} 项" if len(events) > 24 else ""))

    vocals = data.get("vocalSegments")
    if isinstance(vocals, list) and vocals:
        lines.append(f"人声段落（浅听）：{_spans(vocals, 14)}。")

    instruments = data.get("instruments") if isinstance(data.get("instruments"), dict) else {}
    instrument_lines = []
    confidences = instruments.get("_confidence") if isinstance(instruments.get("_confidence"), dict) else {}
    for name, spans in instruments.items():
        if name.startswith("_") or not isinstance(spans, list) or not spans:
            continue
        confidence_item = confidences.get(name)
        if isinstance(confidence_item, dict):
            confidence_value = confidence_item.get("avg_prob")
        else:
            confidence_value = confidence_item
        suffix = ""
        if confidence_value is not None:
            suffix = f"，置信度 {_number(float(confidence_value) * 100, 0)}%"
        instrument_lines.append(f"{name}: {_spans(spans)}{suffix}")
    if instrument_lines:
        lines.append("识别到的声部／乐器：" + "；".join(instrument_lines[:14]) + "。")

    stem_timeline = data.get("stemTimeline") if isinstance(data.get("stemTimeline"), dict) else {}
    stem_names = {"vocals": "人声", "drums": "鼓", "bass": "贝斯", "guitar": "吉他", "piano": "钢琴", "other": "其它"}
    if stem_timeline:
        stem_parts = [
            f"{stem_names.get(name, name)}: {_spans(spans)}"
            for name, spans in stem_timeline.items() if isinstance(spans, list) and spans
        ]
        if stem_parts:
            lines.append("分轨活动：" + "；".join(stem_parts) + "。")

    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    if notes:
        pitches = [int(item.get("pitch")) for item in notes if isinstance(item, dict) and str(item.get("pitch", "")).lstrip("-").isdigit()]
        names = [str(item.get("note_name")) for item in notes if isinstance(item, dict) and item.get("note_name")]
        note_summary = f"音符事件 {len(notes)} 个"
        if pitches:
            note_summary += f"；音域 {_note_name_from_midi(min(pitches))}–{_note_name_from_midi(max(pitches))}"
        if names:
            note_summary += "；常见音 " + "、".join(f"{name}×{count}" for name, count in Counter(names).most_common(8))
        lines.append(note_summary + "。")
        try:
            total_seconds = max(1, int(float(duration or 0)))
            density = []
            for start in range(0, total_seconds + 1, 10):
                count = sum(
                    1 for item in notes if isinstance(item, dict)
                    and start <= float(item.get("start") or 0) < start + 10
                )
                density.append(f"{_mmss(start)} {count}")
            lines.append("每 10 秒音符密度：" + "；".join(density[:36]) + ("；后段省略" if len(density) > 36 else "") + "。")
        except (TypeError, ValueError):
            pass

    stem_notes = data.get("stemNotes") if isinstance(data.get("stemNotes"), dict) else {}
    if stem_notes:
        parts = []
        for stem, values in stem_notes.items():
            if not isinstance(values, list):
                continue
            pitches = [item.get("pitch") for item in values if isinstance(item, dict) and item.get("pitch") is not None]
            span = f"，{_note_name_from_midi(min(pitches))}–{_note_name_from_midi(max(pitches))}" if pitches else ""
            parts.append(f"{stem_names.get(stem, stem)} {len(values)} 音{span}")
        if parts:
            lines.append("分轨音高：" + "；".join(parts) + "。")

    timbre = data.get("voiceTimbre") if isinstance(data.get("voiceTimbre"), dict) else {}
    if timbre:
        labels = timbre.get("labels") if isinstance(timbre.get("labels"), dict) else {}
        quality = timbre.get("voice_quality") if isinstance(timbre.get("voice_quality"), dict) else {}
        pitch = timbre.get("pitch") if isinstance(timbre.get("pitch"), dict) else {}
        lines.append(
            "人声音色："
            + str(timbre.get("timbre_string") or " / ".join(str(value) for value in labels.values()) or "已测量")
            + f"；中位基频 {_number(pitch.get('f0_median'), 0)} Hz；HNR {_number(quality.get('hnr_db'))} dB；"
            + f"jitter {_number(quality.get('jitter'), 4)}；shimmer {_number(quality.get('shimmer'), 4)}。"
        )

    texture = data.get("voiceTexture") if isinstance(data.get("voiceTexture"), dict) else {}
    if texture:
        overall = texture.get("overall") if isinstance(texture.get("overall"), dict) else {}
        by_type = texture.get("by_type") if isinstance(texture.get("by_type"), dict) else {}
        parts = []
        for kind, detail in by_type.items():
            if isinstance(detail, dict):
                parts.append(f"{kind} {detail.get('duration_pct', '?')}%/{detail.get('duration_s', '?')}s")
        lines.append(
            f"声音纹理：{texture.get('texture_label') or '?'}；中位音高跳动 IQR {overall.get('median_iqr', '?')}；"
            f"中位连续发声比例 {overall.get('median_voiced_ratio', '?')}；组成 " + "、".join(parts) + "。"
        )
        texture_segments = texture.get("segments") if isinstance(texture.get("segments"), list) else []
        if texture_segments:
            lines.append(
                "声音纹理时间轴：" + "；".join(
                    f"{_mmss(item.get('start'))}–{_mmss(item.get('end'))} {item.get('type')}"
                    f"（F0 {item.get('median_f0', '?')}Hz，发声 {item.get('voiced_ratio', '?')}）"
                    for item in texture_segments[:32] if isinstance(item, dict)
                ) + (f"；另 {len(texture_segments) - 32} 段" if len(texture_segments) > 32 else "") + "。"
            )

    voice_profile = data.get("voiceProfile") if isinstance(data.get("voiceProfile"), dict) else {}
    if voice_profile:
        soft = voice_profile.get("softWindow") if isinstance(voice_profile.get("softWindow"), dict) else {}
        burst = voice_profile.get("burstWindow") if isinstance(voice_profile.get("burstWindow"), dict) else {}
        parts = []
        if soft:
            parts.append(f"轻声窗 {_mmss(soft.get('start'))}，气息噪声 {_number(float(soft.get('breathNoiseRatio') or 0) * 100, 1)}%，空气感 {_number(float(soft.get('airRatio') or 0) * 100, 1)}%")
        if burst:
            parts.append(f"强声窗 {_mmss(burst.get('start'))}，气息噪声 {_number(float(burst.get('breathNoiseRatio') or 0) * 100, 1)}%，空气感 {_number(float(burst.get('airRatio') or 0) * 100, 1)}%")
        if voice_profile.get("loudnessRatio") is not None:
            parts.append(f"强弱比 {_number(voice_profile.get('loudnessRatio'))}×")
        if voice_profile.get("tailReverb") is not None:
            parts.append(f"混响尾 {_number(voice_profile.get('tailReverb'))}s")
        if parts:
            lines.append("人声轮廓：" + "；".join(parts) + "。")

    vibrato = data.get("vibrato") if isinstance(data.get("vibrato"), dict) else {}
    if vibrato:
        if vibrato.get("detected"):
            lines.append(
                f"颤音：检测到；平均速率 {_number(vibrato.get('average_rate'))} Hz；"
                f"平均深度 {_number(vibrato.get('average_depth_cents'))} cents；"
                f"持续片段 {vibrato.get('segment_count', '?')}。"
            )
        else:
            lines.append("颤音：未检测到稳定颤音，或样本不足。")

    speech = data.get("speechAnalysis") if isinstance(data.get("speechAnalysis"), dict) else {}
    if speech:
        pause = speech.get("pause") if isinstance(speech.get("pause"), dict) else {}
        intonation = speech.get("intonation") if isinstance(speech.get("intonation"), dict) else {}
        rhythm = speech.get("rhythm") if isinstance(speech.get("rhythm"), dict) else {}
        lines.append(
            f"发声节奏：估算 {_number(speech.get('speech_rate_syllables_per_min'), 0)} 音节/分（{speech.get('rate_label', '?')}）；"
            f"停顿 {pause.get('count', '?')} 次、占比 {_number(float(pause.get('ratio') or 0) * 100, 0)}%（{pause.get('label', '?')}）；"
            f"语调范围 {_number(intonation.get('f0_range_semitones'))} 半音（{intonation.get('label', '?')}）；"
            f"节奏 {rhythm.get('label', '?')}。"
        )

    phrase = data.get("phraseDynamics") if isinstance(data.get("phraseDynamics"), list) else []
    if phrase:
        labels = Counter(str(item.get("label") or item.get("trend") or "unknown") for item in phrase if isinstance(item, dict))
        lines.append("五秒短语动态：" + "、".join(f"{name}×{count}" for name, count in labels.most_common()) + "。")

    lyrics = data.get("lyrics") if isinstance(data.get("lyrics"), dict) else {}
    lyric_segments = lyrics.get("segments") if isinstance(lyrics.get("segments"), list) else []
    if lyric_segments:
        lines.append(f"本机 Whisper 听写（语言 {lyrics.get('language') or '?'}；请以你的耳朵为准）：")
        lyric_chars = 0
        for item in lyric_segments:
            if not isinstance(item, dict):
                continue
            line = f"[{_mmss(item.get('start'))}–{_mmss(item.get('end'))}] {str(item.get('text') or '').strip()}"
            if lyric_chars + len(line) > 10000:
                lines.append(f"[听写过长，余下 {max(0, len(lyric_segments) - lyric_segments.index(item))} 段保留在完整 JSON 报告中]")
                break
            lines.append(line)
            lyric_chars += len(line)
    elif isinstance(lyrics.get("lrc"), str) and lyrics.get("lrc").strip():
        lines.append("歌词时间轴（外部歌词来源，非模型听写）：")
        lines.append(lyrics["lrc"].strip()[:10000])

    lines.append(
        "阅读要求：只能把这些数值当作声学证据；不要根据音色推断身份、健康、年龄或人格，"
        "也不要把自动歌词当成用户真正唱出的逐字原文。"
    )
    summary = "\n".join(line for line in lines if line).strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS - 80].rstrip() + "\n[报告已按上下文上限截短；完整 JSON 仍保存在本机。]"
    return summary


class OceanListenService:
    """One-at-a-time Ocean worker with durable attachment status."""

    def __init__(
        self,
        attachments: AttachmentService | None = None,
        *,
        source_dir: Path | None = None,
        data_dir: Path | None = None,
        runtime_dir: Path | None = None,
        python_path: Path | None = None,
    ) -> None:
        self.attachments = attachments or attachment_service
        self.source_dir = Path(source_dir or OCEAN_SOURCE_DIR)
        self.data_dir = Path(data_dir or OCEAN_DATA_DIR)
        self.storage_root = self.data_dir.parent
        self.runtime_dir = Path(runtime_dir or OCEAN_RUNTIME_DIR)
        configured_python = str(os.getenv("OCEAN_LISTEN_PYTHON") or "").strip()
        self.python_path = Path(python_path or configured_python or (self.runtime_dir / ".venv" / "bin" / "python"))
        self.ready_marker = self.runtime_dir / "ready.json"
        self.install_log = self.runtime_dir / "install.log"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocean-listen")
        self._lock = threading.RLock()
        self._futures: dict[str, Future[Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._install_thread: threading.Thread | None = None
        self._install_process: subprocess.Popen[str] | None = None
        self._closed = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def installed(self) -> bool:
        if not self.python_path.is_file() or not (self.source_dir / "ocean.py").is_file():
            return False
        runtime_ffmpeg = self.runtime_dir / "bin" / "ffmpeg"
        if not runtime_ffmpeg.is_file() and not shutil.which("ffmpeg"):
            return False
        try:
            marker = json.loads(self.ready_marker.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(marker, dict) and marker.get("ready") is True

    def start(self) -> None:
        """Allow a new FastAPI lifespan after a clean test/dev shutdown."""
        with self._lock:
            if not self._closed:
                return
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ocean-listen"
            )
            self._closed = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            installing = bool(self._install_thread and self._install_thread.is_alive())
            queued = sum(1 for future in self._futures.values() if not future.done())
        installed = self.installed()
        supported = platform.system() == "Darwin"
        if installing:
            detail = "正在准备独立 Python 3.10、本机 FFmpeg 与听海模型依赖；不要求 Homebrew。"
        elif installed:
            detail = "本机听觉已就绪；音频会逐个分析，原文件不上传给模型供应商。"
        elif supported:
            detail = "尚未安装本机听觉。点一次即可安装独立 Python、FFmpeg 与模型；不要求 Homebrew。首次下载会比较大。"
        else:
            detail = "自动安装器仅面向 macOS；代码与测试仍可在其他系统运行。"
        return {
            "engine": "ocean-listen",
            "source_version": "928dfba62a2c074ccb0154f7ddd42743e4ce9e75",
            "platform": platform.system(),
            "supported": supported,
            "installed": installed,
            "installing": installing,
            "queued": queued,
            "detail": detail,
            "install_log_tail": self._tail(self.install_log, 6000),
            "privacy": "原音频、本机 Whisper 与完整 JSON 均留在 Mac；只把压缩报告交给当前模型。",
        }

    def health(self) -> dict[str, str]:
        status = self.status()
        if status["installed"]:
            return {"health": "ok", "detail": f"本机听觉就绪；队列 {status['queued']} 项"}
        if status["installing"]:
            return {"health": "degraded", "detail": "正在安装独立听海环境"}
        return {"health": "degraded", "detail": "等待使用者在系统页确认首次安装"}

    @staticmethod
    def _tail(path: Path, limit: int) -> str:
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        return raw[-limit:].decode("utf-8", "replace")

    def start_install(self) -> dict[str, Any]:
        if platform.system() != "Darwin":
            raise RuntimeError("听海一键安装目前只支持 macOS")
        if self.installed():
            return self.status()
        if not OCEAN_INSTALL_SCRIPT.is_file():
            raise RuntimeError("听海安装脚本缺失")
        with self._lock:
            if self._install_thread and self._install_thread.is_alive():
                return self.status()
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.install_log.write_text("", encoding="utf-8")
            self._install_thread = threading.Thread(
                target=self._run_installer,
                name="ocean-listen-installer",
                daemon=True,
            )
            self._install_thread.start()
        return self.status()

    def _run_installer(self) -> None:
        env = os.environ.copy()
        env.update({
            "JTYHOME_DATA_DIR": str(self.storage_root),
            "OCEAN_RUNTIME_DIR": str(self.runtime_dir),
            "PYTHONUNBUFFERED": "1",
        })
        try:
            with self.install_log.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    ["/bin/bash", str(OCEAN_INSTALL_SCRIPT), "--service"],
                    cwd=str(SOURCE_ROOT),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                with self._lock:
                    self._install_process = process
                return_code = process.wait()
                if return_code != 0:
                    log.write(f"\n[听海] 安装失败，退出码 {return_code}\n")
        except Exception as exc:
            with self.install_log.open("a", encoding="utf-8") as log:
                log.write(f"\n[听海] 安装器异常：{type(exc).__name__}: {exc}\n")
        finally:
            with self._lock:
                self._install_process = None
            if self.installed():
                self.reconcile()

    def register(self, attachment_id: str) -> dict[str, Any] | None:
        item = self.attachments.get_internal(attachment_id)
        if not item or item.get("kind") != "audio":
            return None
        if self.installed():
            self.attachments.update_meta(attachment_id, {
                "status": "analysis_queued",
                "parse_message": "音频已保存在本机，已进入听海队列。",
                "analysis_status": "queued",
                "analysis_stage": "排队等待",
                "analysis_progress": 1,
                "analysis_error": "",
            })
            self.enqueue(attachment_id)
        else:
            self.attachments.update_meta(attachment_id, {
                "status": "analysis_waiting",
                "parse_message": "音频已保存在本机；请到系统页安装一次“听海 · 本机听觉”。",
                "analysis_status": "waiting_install",
                "analysis_stage": "等待安装本机听觉",
                "analysis_progress": 0,
                "analysis_error": "",
            })
        return self.attachments.get(attachment_id)

    def reconcile(self) -> int:
        if not self.installed() or self._closed:
            return 0
        queued = 0
        for meta_path in UPLOAD_DIR.glob("*.json"):
            try:
                raw = json.loads(meta_path.read_text("utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("kind") != "audio":
                continue
            status = str(raw.get("analysis_status") or "")
            if status in {"waiting_install", "queued", "running"}:
                self.attachments.update_meta(str(raw.get("id") or ""), {
                    "status": "analysis_queued",
                    "parse_message": "听海已就绪，音频已进入本机分析队列。",
                    "analysis_status": "queued",
                    "analysis_stage": "排队等待",
                    "analysis_progress": max(1, int(raw.get("analysis_progress") or 0)),
                    "analysis_error": "",
                })
                if self.enqueue(str(raw.get("id") or "")):
                    queued += 1
        return queued

    def enqueue(self, attachment_id: str, *, force: bool = False) -> bool:
        if self._closed or not self.installed():
            return False
        item = self.attachments.get_internal(attachment_id)
        if not item or item.get("kind") != "audio":
            return False
        if item.get("analysis_status") == "ready" and not force:
            return False
        with self._lock:
            current = self._futures.get(attachment_id)
            if current and not current.done():
                return False
            self.attachments.update_meta(attachment_id, {
                "status": "analysis_queued",
                "parse_message": "音频已进入听海队列。",
                "analysis_status": "queued",
                "analysis_stage": "排队等待",
                "analysis_progress": 1,
                "analysis_error": "",
            })
            future = self._executor.submit(self._analyze, attachment_id)
            self._futures[attachment_id] = future
            future.add_done_callback(lambda _: self._drop_future(attachment_id))
        return True

    def retry(self, attachment_id: str) -> dict[str, Any]:
        item = self.attachments.get_internal(attachment_id)
        if not item or item.get("kind") != "audio":
            raise FileNotFoundError("音频附件不存在")
        if not self.installed():
            self.register(attachment_id)
            return self.attachments.get(attachment_id) or {}
        self.enqueue(attachment_id, force=True)
        return self.attachments.get(attachment_id) or {}

    def _drop_future(self, attachment_id: str) -> None:
        with self._lock:
            future = self._futures.get(attachment_id)
            if future and future.done():
                self._futures.pop(attachment_id, None)

    def _artifact_dir(self, attachment_id: str) -> Path:
        return self.data_dir / attachment_id

    def _set_stage(self, attachment_id: str, stage: str, progress: int) -> None:
        self.attachments.update_meta(attachment_id, {
            "analysis_stage": stage,
            "analysis_progress": max(1, min(99, int(progress))),
            "parse_message": f"听海正在本机分析：{stage}（{max(1, min(99, int(progress)))}%）",
        })

    @staticmethod
    def _stage_from_line(line: str) -> tuple[str, int] | None:
        lowered = line.lower()
        stages = (
            (("analyzing structure", "shallow listen"), "读取节奏、调性与能量", 10),
            (("extracting midi", "extracting notes", "listening to"), "提取音高与音符", 22),
            (("detecting instruments", "panns"), "识别乐器与声部", 32),
            (("generating spectrogram",), "绘制频谱图", 40),
            (("pre-classification", "detected:"), "判断声音类型", 46),
            (("separating stems", "demucs"), "分离人声与伴奏", 56),
            (("per-stem midi", "building stem timeline"), "整理分轨时间轴", 67),
            (("voice profile", "f0 trajectory"), "分析人声轮廓与音准轨迹", 75),
            (("voice timbre", "segmenting voice", "speech patterns"), "分析音色、唱法与节奏", 83),
            (("lyrics (whisper)", "loading whisper", "transcribing"), "本机听写歌词", 90),
            (("json ->", "done. full analysis"), "整理听觉报告", 97),
        )
        for needles, label, progress in stages:
            if any(needle in lowered for needle in needles):
                return label, progress
        return None

    def _append_analysis_log(self, path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
        try:
            if path.stat().st_size > MAX_LOG_BYTES:
                raw = path.read_bytes()[-MAX_LOG_BYTES:]
                path.write_bytes(raw)
        except OSError:
            pass

    def _analyze(self, attachment_id: str) -> None:
        item = self.attachments.get_internal(attachment_id)
        if not item:
            return
        artifact_dir = self._artifact_dir(attachment_id)
        cache_dir = artifact_dir / "cache"
        report_path = artifact_dir / "report.json"
        log_path = artifact_dir / "analysis.log"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        self.attachments.update_meta(attachment_id, {
            "status": "analysis_running",
            "parse_message": "听海正在本机读取音频。",
            "analysis_status": "running",
            "analysis_stage": "打开声音",
            "analysis_progress": 3,
            "analysis_error": "",
            "analysis_started_at": _utc_now(),
            "analysis_finished_at": None,
        })

        command = [
            str(self.python_path), str(self.source_dir / "ocean.py"), str(item["path"]),
            "--deep", "--mode", "auto", "--lyric", "whisper", "--language", "auto",
            "--whisper-model", "small", "--output", str(report_path),
            "--cache-dir", str(cache_dir), "--force",
        ]
        env = os.environ.copy()
        cache_root = self.runtime_dir / "cache"
        runtime_bin = self.runtime_dir / "bin"
        ffmpeg_path = runtime_bin / "ffmpeg"
        env.update({
            "PYTHONUNBUFFERED": "1",
            "PATH": f"{runtime_bin}:{self.python_path.parent}:{env.get('PATH', '')}",
            "IMAGEIO_FFMPEG_EXE": str(ffmpeg_path) if ffmpeg_path.is_file() else env.get("IMAGEIO_FFMPEG_EXE", ""),
            "MPLCONFIGDIR": str(cache_root / "matplotlib"),
            "HF_HOME": str(cache_root / "huggingface"),
            "TORCH_HOME": str(cache_root / "torch"),
            "XDG_CACHE_HOME": str(cache_root),
            "OMP_NUM_THREADS": "2",
        })
        cache_root.mkdir(parents=True, exist_ok=True)
        last_stage = ""
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.source_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                start_new_session=True,
            )
            with self._lock:
                self._processes[attachment_id] = process
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                self._append_analysis_log(log_path, line)
                stage = self._stage_from_line(line)
                if stage and stage[0] != last_stage:
                    last_stage = stage[0]
                    self._set_stage(attachment_id, stage[0], stage[1])
                if process.poll() is not None:
                    break
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"Ocean Listen 退出码 {return_code}")
            if not report_path.is_file():
                raise RuntimeError("Ocean Listen 没有生成 JSON 报告")
            data = json.loads(report_path.read_text("utf-8"))
            summary = build_ocean_summary(data, filename=str(item.get("name") or "音频"))
            spectrogram = self._find_spectrogram(data, artifact_dir)
            report_rel = str(report_path.resolve().relative_to(self.storage_root.resolve()))
            spectrogram_rel = (
                str(spectrogram.resolve().relative_to(self.storage_root.resolve())) if spectrogram else ""
            )
            self.attachments.update_meta(attachment_id, {
                "status": "analyzed",
                "parse_message": "听海已在本机听完；结构化报告可交给所有模型。",
                "analysis_status": "ready",
                "analysis_stage": "已听完",
                "analysis_progress": 100,
                "analysis_error": "",
                "analysis_finished_at": _utc_now(),
                "analysis_report_rel": report_rel,
                "analysis_spectrogram_rel": spectrogram_rel,
                "analysis_log_rel": str(log_path.resolve().relative_to(self.storage_root.resolve())),
                "extracted_text": summary,
                "extracted_chars": len(summary),
                "truncated": len(summary) >= MAX_SUMMARY_CHARS,
            })
        except Exception as exc:
            tail = self._tail(log_path, 5000)
            detail = self._public_error(exc, tail)
            if self.attachments.get(attachment_id):
                self.attachments.update_meta(attachment_id, {
                    "status": "analysis_error",
                    "parse_message": f"听海没有听完：{detail}",
                    "analysis_status": "error",
                    "analysis_stage": "分析失败",
                    "analysis_error": detail,
                    "analysis_finished_at": _utc_now(),
                })
        finally:
            with self._lock:
                self._processes.pop(attachment_id, None)

    @staticmethod
    def _public_error(exc: Exception, log_tail: str) -> str:
        lowered = log_tail.lower()
        if "ffmpeg" in lowered and ("not found" in lowered or "no such file" in lowered):
            return "Mac 没有找到 FFmpeg，请重新运行本机听觉安装。"
        if "no module named" in lowered:
            match = re.findall(r"No module named ['\"]?([^'\"\s]+)", log_tail, re.I)
            return f"独立环境缺少 {match[-1] if match else '分析组件'}，请重新安装本机听觉。"
        if "out of memory" in lowered or "cannot allocate memory" in lowered:
            return "本机内存不足。请关闭占内存的软件后点“重新分析”。"
        if "killed" in lowered:
            return "分析进程被系统中止，通常是内存压力过高。"
        return str(exc or "未知错误")[:500]

    @staticmethod
    def _find_spectrogram(data: dict[str, Any], artifact_dir: Path) -> Path | None:
        candidate = str(data.get("spectrogram") or "").strip()
        possibilities = [Path(candidate)] if candidate else []
        possibilities.extend((artifact_dir / "cache").glob("*_analysis.png"))
        root = artifact_dir.resolve()
        for path in possibilities:
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_file() and resolved.suffix.lower() == ".png":
                return resolved
        return None

    def report_path(self, attachment_id: str) -> Path | None:
        return self._safe_artifact_path(attachment_id, "analysis_report_rel", ".json")

    def spectrogram_path(self, attachment_id: str) -> Path | None:
        return self._safe_artifact_path(attachment_id, "analysis_spectrogram_rel", ".png")

    def analysis_log_tail(self, attachment_id: str, limit: int = 8000) -> str:
        path = self._safe_artifact_path(attachment_id, "analysis_log_rel", ".log")
        return self._tail(path, limit) if path else ""

    def _safe_artifact_path(self, attachment_id: str, field: str, suffix: str) -> Path | None:
        item = self.attachments.get_internal(attachment_id)
        if not item or item.get("kind") != "audio":
            return None
        relative = str(item.get(field) or "").strip()
        if not relative:
            return None
        path = (self.storage_root / relative).resolve()
        try:
            path.relative_to(self.storage_root.resolve())
        except ValueError:
            return None
        return path if path.is_file() and path.suffix.lower() == suffix else None

    def cancel_and_delete(self, attachment_id: str) -> None:
        with self._lock:
            future = self._futures.pop(attachment_id, None)
            process = self._processes.get(attachment_id)
        if future:
            future.cancel()
        self._terminate_process(process)
        artifact_dir = self._artifact_dir(attachment_id)
        if artifact_dir.is_dir():
            shutil.rmtree(artifact_dir, ignore_errors=True)

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            processes = list(self._processes.values())
            installer = self._install_process
        for process in [*processes, *([installer] if installer else [])]:
            self._terminate_process(process)
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str] | None) -> None:
        if not process or process.poll() is not None:
            return
        try:
            pid = int(process.pid)
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (AttributeError, OSError, TypeError, ValueError):
            try:
                process.terminate()
            except OSError:
                pass


ocean_listen = OceanListenService()
