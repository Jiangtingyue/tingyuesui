"""Resolve and proxy album artwork for Life music cards.

The resolver only reads public http/https resources. Every redirect is revalidated
through webpage_service's SSRF guard, so a music URL or artwork redirect cannot
bounce into localhost/LAN/cloud metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

import webpage_service
from runtime_paths import DATA_DIR

USER_AGENT = "JTYHome-MusicArtwork/8.7 (+local companion)"
MAX_HTML_BYTES = 1_500_000
MAX_IMAGE_BYTES = 7_000_000
MAX_REDIRECTS = 4
META_TTL_SECONDS = 24 * 60 * 60
CACHE_DIR = DATA_DIR / "music-artwork"
META_DIR = CACHE_DIR / "meta"
IMAGE_DIR = CACHE_DIR / "images"
META_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


class _ArtworkMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "meta" or self.image:
            return
        data = {str(k).lower(): str(v or "") for k, v in attrs}
        key = (data.get("property") or data.get("name") or "").strip().lower()
        if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            self.image = (data.get("content") or "").strip()


def _safe_url(url: str) -> str:
    # Reuse the project's existing DNS/IP guard instead of creating a second,
    # subtly different SSRF policy for album art.
    return webpage_service._normalized_public_url(str(url or ""))


def _read_limited(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise ValueError("resource exceeds artwork size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _get_following_redirects(url: str, *, accept: str, byte_limit: int) -> tuple[bytes, str, str]:
    current = _safe_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    with httpx.Client(timeout=httpx.Timeout(9.0, connect=4.0), headers=headers, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            current = _safe_url(current)
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect has no location")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                raw = _read_limited(response, byte_limit)
                return raw, str(response.url), (response.headers.get("content-type") or "").lower()
    raise ValueError("too many redirects")


def _provider_oembed(music_url: str) -> str:
    host = (urlsplit(music_url).hostname or "").lower()
    if host == "open.spotify.com" or host.endswith(".spotify.com"):
        return f"https://open.spotify.com/oembed?url={quote(music_url, safe='')}"
    if host in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"} or host.endswith(".youtube.com"):
        return f"https://www.youtube.com/oembed?url={quote(music_url, safe='')}&format=json"
    return ""


def _resolve_uncached(music_url: str) -> str:
    safe_music_url = _safe_url(music_url)

    # Spotify and YouTube expose a compact, stable thumbnail field; use that
    # before downloading a much larger provider page.
    oembed = _provider_oembed(safe_music_url)
    if oembed:
        try:
            raw, _, ctype = _get_following_redirects(
                oembed,
                accept="application/json,text/plain;q=0.5,*/*;q=0.1",
                byte_limit=300_000,
            )
            if "json" in ctype or raw.lstrip().startswith(b"{"):
                payload = json.loads(raw.decode("utf-8", "replace"))
                candidate = str(payload.get("thumbnail_url") or "").strip()
                if candidate:
                    return _safe_url(candidate)
        except Exception:
            pass

    raw, final_url, ctype = _get_following_redirects(
        safe_music_url,
        accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        byte_limit=MAX_HTML_BYTES,
    )
    if "html" not in ctype and "xhtml" not in ctype:
        raise ValueError("music page is not html")
    parser = _ArtworkMetaParser()
    parser.feed(raw.decode("utf-8", "replace"))
    if not parser.image:
        raise ValueError("music page has no album artwork metadata")
    return _safe_url(urljoin(final_url, parser.image))


def resolve_artwork_url(music_url: str) -> str:
    music_url = str(music_url or "").strip()
    if not music_url:
        return ""
    digest = hashlib.sha256(music_url.encode("utf-8", "ignore")).hexdigest()
    target = META_DIR / f"{digest}.json"
    now = int(time.time())
    try:
        cached = json.loads(target.read_text("utf-8"))
        if now - int(cached.get("at") or 0) < META_TTL_SECONDS:
            return str(cached.get("artwork_url") or "")
    except Exception:
        pass

    artwork_url = ""
    try:
        artwork_url = _resolve_uncached(music_url)
    except Exception:
        artwork_url = ""
    payload = {"at": now, "music_url": music_url, "artwork_url": artwork_url}
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    os.replace(temp, target)
    return artwork_url


def artwork_bytes(music_url: str) -> tuple[bytes, str]:
    artwork_url = resolve_artwork_url(music_url)
    if not artwork_url:
        raise FileNotFoundError("no artwork")
    digest = hashlib.sha256(artwork_url.encode("utf-8", "ignore")).hexdigest()
    meta_path = IMAGE_DIR / f"{digest}.json"
    data_path = IMAGE_DIR / f"{digest}.bin"
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        if data_path.exists() and int(time.time()) - int(meta.get("at") or 0) < META_TTL_SECONDS:
            return data_path.read_bytes(), str(meta.get("content_type") or "image/jpeg")
    except Exception:
        pass

    raw, _, ctype = _get_following_redirects(
        artwork_url,
        accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.1",
        byte_limit=MAX_IMAGE_BYTES,
    )
    content_type = ctype.split(";", 1)[0].strip()
    if not content_type.startswith("image/"):
        raise ValueError("artwork resource is not an image")
    temp = data_path.with_name(f".{data_path.name}.{os.getpid()}.tmp")
    temp.write_bytes(raw)
    os.replace(temp, data_path)
    meta_temp = meta_path.with_name(f".{meta_path.name}.{os.getpid()}.tmp")
    meta_temp.write_text(json.dumps({"at": int(time.time()), "content_type": content_type}), "utf-8")
    os.replace(meta_temp, meta_path)
    return raw, content_type
