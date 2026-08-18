"""Small, source-aware personality cards distilled from private artifacts.

The original HTML pages and letters never enter the model prompt.  This deck
only contributes a few short behavioural summaries when the current message
actually matches their topic.  Shared fiction stays explicitly labelled as
fiction so a model cannot turn a scenario name into its permanent identity.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from config import PERSONA_CARD_CONFIG, USER_NAME, USER_NICKNAME


class PersonaCardDeck:
    def __init__(self) -> None:
        self._cards: list[dict[str, Any]] = []
        self._mtime: float | None = None
        self._error = ""

    @property
    def path(self) -> Path:
        return Path(str(PERSONA_CARD_CONFIG.get("path") or ""))

    @staticmethod
    def _clean_text(value: Any, limit: int = 900) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:limit]

    def load(self, refresh: bool = False) -> list[dict[str, Any]]:
        if not PERSONA_CARD_CONFIG.get("enabled", True):
            self._cards = []
            self._error = ""
            return []
        configured_path = self.path
        default_path = Path(__file__).with_name("persona_cards.defaults.json")
        path = configured_path if configured_path.is_file() else default_path
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._cards = []
            self._mtime = None
            self._error = "人格卡文件不存在（可选）"
            return []
        if not refresh and self._mtime == mtime:
            return self._cards
        try:
            payload = json.loads(path.read_text("utf-8"))
            raw_cards = payload.get("cards", []) if isinstance(payload, dict) else []
            cards: list[dict[str, Any]] = []
            for raw in raw_cards:
                if not isinstance(raw, dict):
                    continue
                card_id = self._clean_text(raw.get("id"), 80)
                summary = self._clean_text(raw.get("summary"), 900)
                if not card_id or not summary:
                    continue
                triggers = []
                for item in raw.get("triggers", []):
                    trigger = self._clean_text(item, 40).lower()
                    if trigger and trigger not in triggers:
                        triggers.append(trigger)
                cards.append({
                    "id": card_id,
                    "scope": self._clean_text(raw.get("scope") or "relationship_style", 40),
                    "always": bool(raw.get("always", False)),
                    "triggers": triggers[:30],
                    "summary": summary,
                    "guidance": self._clean_text(raw.get("guidance"), 900),
                    "reality": self._clean_text(raw.get("reality"), 360),
                })
            self._cards = cards[:80]
            self._mtime = mtime
            self._error = "" if path == configured_path else "正在使用内置最小人格卡"
        except Exception:
            self._cards = []
            self._mtime = mtime
            self._error = "人格卡读取失败，请检查 JSON 格式"
        return self._cards

    @staticmethod
    def _score(card: dict[str, Any], query: str) -> int:
        query_lower = query.lower()
        return sum(
            3 + min(len(trigger), 8)
            for trigger in card.get("triggers", [])
            if trigger and trigger in query_lower
        )

    def selected(self, query: str) -> list[dict[str, Any]]:
        cards = self.load()
        maximum = max(1, min(int(PERSONA_CARD_CONFIG.get("max_cards", 3)), 8))
        always = [card for card in cards if card.get("always")]
        ranked = sorted(
            (
                (self._score(card, query or ""), index, card)
                for index, card in enumerate(cards)
                if not card.get("always")
            ),
            key=lambda item: (-item[0], item[1]),
        )
        matched = [card for score, _index, card in ranked if score > 0]
        result: list[dict[str, Any]] = []
        for card in always + matched:
            if card not in result:
                result.append(card)
            if len(result) >= maximum:
                break
        return result

    def prompt_context(self, query: str) -> str:
        selected = self.selected(query)
        if not selected:
            return ""
        budget = max(500, min(int(PERSONA_CARD_CONFIG.get("max_chars", 1800)), 6000))
        lines = [
            "<artifact_persona_cards>",
            f"这些是{USER_NAME}（{USER_NICKNAME}）从旧作品中确认的相处证据，不是要求复读的台词。只在当前话题相关时自然体现。",
        ]
        for card in selected:
            parts = [f"[{card['scope']}:{card['id']}] {card['summary']}"]
            if card.get("guidance"):
                parts.append(f"回应方向：{card['guidance']}")
            if card.get("reality"):
                parts.append(f"现实边界：{card['reality']}")
            candidate = " ".join(parts)
            if len("\n".join(lines + [candidate, "</artifact_persona_cards>"])) > budget:
                break
            lines.append(candidate)
        lines.extend([
            f"共同剧本只在{USER_NICKNAME}主动进入时使用；不得覆盖当前模型身份，也不得把未来信件当作已经发生的事实。",
            "</artifact_persona_cards>",
        ])
        return "\n".join(lines)[:budget]

    def health(self) -> dict[str, Any]:
        cards = self.load(refresh=True)
        if self._error and not cards:
            return {"health": "warn", "detail": self._error, "cards": 0}
        return {
            "health": "ok",
            "detail": (
                f"{len(cards)} 张人格卡；每轮最多 {PERSONA_CARD_CONFIG.get('max_cards', 3)} 张"
                + (f"（{self._error}）" if self._error else "")
            ),
            "cards": len(cards),
        }


persona_cards = PersonaCardDeck()
