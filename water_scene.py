"""Embedded backend contract for the Hydrangea interactive-water scene.

The browser owns rendering and pointer input.  This module keeps the scene
fully same-origin by verifying its bundled assets and negotiating a bounded
runtime profile without storing client telemetry or personal data.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parent
_ASSET_ROOT = _ROOT / "static" / "hydrangea-water-hero"
_ASSETS = (
    ("image", "flower-sea.png", "image/png"),
    ("style", "hero.css", "text/css"),
    ("renderer", "water-refraction.runtime.js", "text/javascript"),
    ("effects", "hero-fx.runtime.js", "text/javascript"),
    ("controller", "hero.runtime.js", "text/javascript"),
)
_PUBLIC_PREFIX = "/static/hydrangea-water-hero/"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def _number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(maximum, parsed))


def _integer(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    return int(round(_number(value, default=default, minimum=minimum, maximum=maximum)))


def _boolean(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


@lru_cache(maxsize=16)
def _fingerprint(path_text: str, modified_ns: int, size: int) -> str:
    del modified_ns, size
    digest = hashlib.sha256()
    with open(path_text, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _asset_manifest() -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for key, filename, media_type in _ASSETS:
        path = _ASSET_ROOT / filename
        item: dict[str, Any] = {
            "url": f"{_PUBLIC_PREFIX}{filename}",
            "media_type": media_type,
            "ready": path.is_file(),
            "bytes": 0,
            "fingerprint": "",
        }
        if path.is_file():
            stat = path.stat()
            item["bytes"] = stat.st_size
            item["fingerprint"] = _fingerprint(
                str(path), int(stat.st_mtime_ns), int(stat.st_size)
            )
        manifest[key] = item
    return manifest


_PROFILES: dict[str, dict[str, Any]] = {
    "high": {
        "sim_width": 192,
        "sim_max_height": 360,
        "max_dpr": 2.0,
        "target_fps": 60,
        "pointer_interval_ms": 7,
        "trail_spacing_px": 4.5,
        "trail_radius": 2.8,
        "trail_strength": 0.48,
        "tap_radius": 5.2,
        "tap_strength": 0.78,
        "max_pointer_samples": 30,
        "ambient_fx": True,
    },
    "balanced": {
        "sim_width": 160,
        "sim_max_height": 320,
        "max_dpr": 1.75,
        "target_fps": 60,
        "pointer_interval_ms": 10,
        "trail_spacing_px": 6.0,
        "trail_radius": 2.5,
        "trail_strength": 0.44,
        "tap_radius": 4.8,
        "tap_strength": 0.72,
        "max_pointer_samples": 24,
        "ambient_fx": True,
    },
    "eco": {
        "sim_width": 128,
        "sim_max_height": 240,
        "max_dpr": 1.25,
        "target_fps": 30,
        "pointer_interval_ms": 16,
        "trail_spacing_px": 9.0,
        "trail_radius": 2.2,
        "trail_strength": 0.38,
        "tap_radius": 4.2,
        "tap_strength": 0.64,
        "max_pointer_samples": 16,
        "ambient_fx": False,
    },
}


def _select_profile(client: dict[str, Any]) -> str:
    requested = str(client.get("quality") or "auto").strip().lower()
    if requested in _PROFILES:
        return requested

    width = _integer(client.get("viewport_width"), default=1280, minimum=240, maximum=8192)
    height = _integer(client.get("viewport_height"), default=720, minimum=240, maximum=8192)
    dpr = _number(client.get("device_pixel_ratio"), default=1.0, minimum=1.0, maximum=4.0)
    cores = _integer(client.get("hardware_concurrency"), default=4, minimum=1, maximum=128)
    memory = _number(client.get("device_memory"), default=4.0, minimum=0.25, maximum=64.0)
    reduced = _boolean(client.get("reduced_motion"))
    save_data = _boolean(client.get("save_data"))
    pixels = width * height * dpr * dpr

    if reduced or save_data or cores <= 2 or memory <= 2 or pixels >= 8_000_000:
        return "eco"
    if cores >= 8 and memory >= 8 and pixels <= 4_500_000:
        return "high"
    return "balanced"


def _profile(client: dict[str, Any]) -> dict[str, Any]:
    name = _select_profile(client)
    values = dict(_PROFILES[name])
    reduced = _boolean(client.get("reduced_motion"))
    webgl = _boolean(client.get("webgl"), default=True)
    pointer = str(client.get("pointer") or "fine").strip().lower()
    if pointer not in {"fine", "coarse", "none"}:
        pointer = "fine"

    if reduced:
        values["target_fps"] = min(int(values["target_fps"]), 30)
        values["ambient_fx"] = False
        values["trail_strength"] = round(float(values["trail_strength"]) * 0.78, 3)
    if pointer == "coarse":
        values["trail_spacing_px"] = round(float(values["trail_spacing_px"]) * 1.18, 2)
        values["tap_radius"] = round(float(values["tap_radius"]) * 1.18, 2)

    return {
        "name": name,
        "renderer": "webgl" if webgl else "image",
        "pointer": pointer,
        "reduced_motion": reduced,
        **values,
    }


def bootstrap(client: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the complete public scene contract for one browser profile."""
    capabilities = client if isinstance(client, dict) else {}
    assets = _asset_manifest()
    enabled = _env_enabled("JTYHOME_WATER_SCENE_ENABLED", True)
    ready = enabled and all(item["ready"] for item in assets.values())
    return {
        "ok": ready,
        "enabled": enabled,
        "version": 2,
        "integration": "embedded-same-origin",
        "privacy": "local-capability-negotiation-only",
        "assets": assets,
        "profile": _profile(capabilities),
        "features": {
            "pointer_events": True,
            "coalesced_pointer_samples": True,
            "multi_touch": True,
            "visibility_pause": True,
            "webgl_context_recovery": True,
            "interactive_image_fallback": True,
        },
    }


def status() -> dict[str, Any]:
    """Small health payload used by diagnostics without client capabilities."""
    payload = bootstrap({})
    return {
        "ok": payload["ok"],
        "enabled": payload["enabled"],
        "version": payload["version"],
        "integration": payload["integration"],
        "assets_ready": {
            key: item["ready"] for key, item in payload["assets"].items()
        },
    }
