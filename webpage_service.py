"""Safe current-turn webpage reader.

Public http/https URLs only.  Redirect targets are re-validated so a public URL
cannot bounce into localhost/LAN/cloud metadata.  Successful text snapshots are
stored with the user-message metadata so later history can replay the same bytes.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from runtime_paths import DATA_DIR

URL_RE = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.I)
MAX_URLS = 2
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARS = 18000
MAX_TOTAL_CHARS = 30000
MAX_REDIRECTS = 4
USER_AGENT = "JTYHome-WebReader/8.1 (+local companion)"
SNAPSHOT_DIR = DATA_DIR / "web-snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas", "template"}:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "div", "article", "section", "main", "li", "br", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas", "template"} and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in {"p", "div", "article", "section", "main", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.parts.append(text + " ")

    def result(self) -> tuple[str, str]:
        title = " ".join(self.title_parts).strip()
        text = "".join(self.parts)
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return title[:300], html.unescape(text).strip()


def extract_urls(text: str) -> list[str]:
    out: list[str] = []
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?，。；：！？、）】》")
        if url not in out:
            out.append(url)
        if len(out) >= MAX_URLS:
            break
    return out


def _normalized_public_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("只允许 http/https 网页")
    if parts.username or parts.password:
        raise ValueError("网页 URL 不允许携带账号信息")
    host = parts.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("不读取本机或局域网页面")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("域名无法解析") from exc
    if not infos:
        raise ValueError("域名无法解析")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("不读取本机、私网或保留地址")
    netloc = host
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def _read_limited(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ValueError("网页正文超过 2 MB 读取上限")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_one(url: str) -> dict:
    original = url
    current = url
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,application/xhtml+xml;q=0.9,*/*;q=0.2"}
    try:
        current = _normalized_public_url(url)
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0), headers=headers, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                current = _normalized_public_url(current)
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("网页重定向没有目标地址")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    ctype = response.headers.get("content-type", "").lower()
                    if not ("text/html" in ctype or "application/xhtml+xml" in ctype or "text/plain" in ctype):
                        raise ValueError(f"链接不是可读网页（{ctype.split(';',1)[0] or 'unknown'}）")
                    raw = _read_limited(response)
                    encoding = response.encoding or "utf-8"
                    text = raw.decode(encoding, "replace")
                    if "html" in ctype or "xhtml" in ctype:
                        parser = _TextExtractor()
                        parser.feed(text)
                        title, body = parser.result()
                    else:
                        title, body = "", text.strip()
                    body = body[:MAX_PAGE_CHARS]
                    if not body:
                        raise ValueError("网页没有提取到可读正文")
                    return {"url": original, "final_url": str(response.url), "title": title, "status": "ok", "text": body, "chars": len(body)}
            raise ValueError("网页重定向次数过多")
    except Exception as exc:
        return {"url": original, "final_url": current, "title": "", "status": "error", "text": "", "chars": 0, "error": str(exc)[:240]}


def fetch_from_text(text: str) -> list[dict]:
    pages: list[dict] = []
    used = 0
    for url in extract_urls(text):
        page = fetch_one(url)
        if page.get("status") == "ok":
            remaining = max(0, MAX_TOTAL_CHARS - used)
            page["text"] = str(page.get("text") or "")[:remaining]
            page["chars"] = len(page["text"])
            used += page["chars"]
        pages.append(page)
        if used >= MAX_TOTAL_CHARS:
            break
    return pages


def _snapshot_hash(page: dict) -> str:
    payload = "\n".join((
        str(page.get("final_url") or page.get("url") or ""),
        str(page.get("title") or ""),
        str(page.get("text") or ""),
    )).encode("utf-8", "ignore")
    return hashlib.sha256(payload).hexdigest()


def persist_pages(pages: list[dict], *, excerpt_chars: int = 420) -> list[dict]:
    """Persist successful full snapshots locally and return history-safe refs."""
    refs: list[dict] = []
    excerpt_chars = max(120, min(int(excerpt_chars or 420), 1200))
    for page in pages or []:
        base = {
            "url": str(page.get("url") or ""),
            "final_url": str(page.get("final_url") or page.get("url") or ""),
            "title": str(page.get("title") or "")[:300],
            "status": str(page.get("status") or "error"),
        }
        if page.get("status") != "ok" or not str(page.get("text") or ""):
            base.update({
                "snapshot_hash": "",
                "excerpt": "",
                "chars": 0,
                "error": str(page.get("error") or "")[:240],
            })
            refs.append(base)
            continue
        digest = _snapshot_hash(page)
        target = SNAPSHOT_DIR / f"{digest}.json"
        payload = {
            **base,
            "status": "ok",
            "snapshot_hash": digest,
            "text": str(page.get("text") or ""),
            "chars": len(str(page.get("text") or "")),
        }
        if not target.exists():
            temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            os.replace(temp, target)
        refs.append({
            **base,
            "status": "ok",
            "snapshot_hash": digest,
            "excerpt": str(page.get("text") or "")[:excerpt_chars],
            "chars": int(page.get("chars") or len(str(page.get("text") or ""))),
        })
    return refs


def _load_snapshot(digest: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", str(digest or "")):
        return {}
    path = SNAPSHOT_DIR / f"{digest}.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def context(pages: list[dict], *, max_total_chars: int = MAX_TOTAL_CHARS) -> str:
    """Full current-turn page text, bounded by the unified budget."""
    if not pages or max_total_chars <= 0:
        return ""
    budget = max(0, min(int(max_total_chars), MAX_TOTAL_CHARS))
    parts = ["【网页读取快照】", "以下内容由本机在用户发送本轮消息时实际读取；失败项不得声称已经看过正文。"]
    used = sum(len(x) for x in parts)
    for index, page in enumerate(pages, 1):
        if used >= budget:
            break
        url = str(page.get("final_url") or page.get("url") or "")
        if page.get("status") != "ok":
            block = f"网页{index}：{url}\n[读取失败：{page.get('error') or '未知原因'}]"
        else:
            title = str(page.get("title") or "（无标题）")
            text = str(page.get("text") or "")
            prefix = f"网页{index}：{title}\nURL：{url}\n<web_text>\n"
            suffix = "\n</web_text>"
            remain = max(0, budget - used - len(prefix) - len(suffix) - 2)
            text = text[:remain]
            block = prefix + text + suffix
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts)[:budget]


def history_context(pages: list[dict]) -> str:
    """Short history reference only; never replay the full webpage body."""
    if not pages:
        return ""
    parts = ["【此前网页引用】"]
    for index, page in enumerate(pages, 1):
        url = str(page.get("final_url") or page.get("url") or "")
        title = str(page.get("title") or "（无标题）")
        if page.get("status") != "ok":
            parts.append(f"网页{index}：{title}\nURL：{url}\n[当时读取失败]")
            continue
        digest = str(page.get("snapshot_hash") or "")
        excerpt = str(page.get("excerpt") or "")[:420]
        parts.append(
            f"网页{index}：{title}\nURL：{url}\n快照：{digest[:16] or 'unknown'}\n短摘录：{excerpt}"
        )
    return "\n\n".join(parts)


def _terms(query: str) -> list[str]:
    text = str(query or "").strip().lower()
    out: list[str] = []
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", text):
        out.append(segment)
        for width in (4, 3, 2):
            if len(segment) >= width:
                out.extend(segment[i:i + width] for i in range(len(segment) - width + 1))
    out.extend(re.findall(r"[a-z0-9_\-]{3,}", text))
    ignored = {"这个", "那个", "什么", "怎么", "可以", "我们", "网页", "链接", "文章", "页面", "刚才"}
    uniq: list[str] = []
    for term in out:
        if term in ignored or term in uniq:
            continue
        uniq.append(term)
        if len(uniq) >= 18:
            break
    return uniq


def _chunks(text: str, target: int = 1000) -> list[str]:
    raw = [x.strip() for x in re.split(r"\n{2,}", str(text or "")) if x.strip()]
    result: list[str] = []
    buf = ""
    for part in raw:
        candidate = f"{buf}\n\n{part}".strip() if buf else part
        if buf and len(candidate) > target:
            result.append(buf)
            buf = part
        else:
            buf = candidate
    if buf:
        result.append(buf)
    return result


def relevant_snapshot_context(page_refs: list[dict], query: str, *, max_total_chars: int = 6000) -> str:
    """Retrieve relevant text from local snapshots without network access."""
    if not page_refs or max_total_chars <= 0:
        return ""
    terms = _terms(query)
    candidates: list[tuple[float, int, dict, str]] = []
    refs = [x for x in page_refs if isinstance(x, dict) and x.get("snapshot_hash")]
    for recency, ref in enumerate(reversed(refs[-12:])):
        snap = _load_snapshot(str(ref.get("snapshot_hash") or ""))
        text = str(snap.get("text") or "")
        if not text:
            continue
        title = str(ref.get("title") or snap.get("title") or "")
        url = str(ref.get("final_url") or ref.get("url") or snap.get("final_url") or "")
        for index, chunk in enumerate(_chunks(text)):
            lower = chunk.lower()
            matched = [term for term in terms if term in lower or term in title.lower()]
            score = sum(1.0 + min(4, lower.count(term)) * .35 for term in matched)
            score += max(0.0, 1.0 - recency * .08)
            if matched:
                score += 2.0
            # Generic "this page/previous article" follow-ups may have no useful
            # lexical term; allow only the most recent local snapshot as fallback.
            if not terms and recency == 0:
                score += 2.0
            if score > 0:
                candidates.append((score - index * .0001, recency, {"title": title, "url": url}, chunk))
    if not candidates:
        lowered_query = str(query or "").lower()
        followup_cues = (
            "刚才", "上面", "前面", "后面", "这篇", "这页", "这个网页", "那个网页",
            "这篇文章", "那篇文章", "链接里", "网页里", "页面里", "第", "段", "标题",
            "above", "previous", "that page", "this page", "article",
        )
        if any(cue in lowered_query for cue in followup_cues) and refs:
            ref = refs[-1]
            snap = _load_snapshot(str(ref.get("snapshot_hash") or ""))
            text = str(snap.get("text") or "")
            if text:
                title = str(ref.get("title") or snap.get("title") or "")
                url = str(ref.get("final_url") or ref.get("url") or snap.get("final_url") or "")
                candidates.append((1.0, 0, {"title": title, "url": url}, text[:max_total_chars]))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    parts = ["【本机网页快照 · 按需摘取】", "以下片段来自此前保存的本机快照，没有重新联网。"]
    used = sum(len(x) for x in parts)
    seen: set[str] = set()
    for _score, _recency, meta, chunk in candidates:
        key = f"{meta['url']}|{chunk[:80]}"
        if key in seen:
            continue
        seen.add(key)
        head = f"网页：{meta['title'] or '（无标题）'}\nURL：{meta['url']}\n"
        remain = max_total_chars - used - len(head) - 2
        if remain < 180:
            break
        excerpt = chunk[:remain]
        parts.append(head + excerpt)
        used += len(head) + len(excerpt) + 2
        if len(parts) >= 6:
            break
    return "\n\n".join(parts)[:max_total_chars]


class WebpageService:
    extract_urls = staticmethod(extract_urls)
    fetch_from_text = staticmethod(fetch_from_text)
    persist_pages = staticmethod(persist_pages)
    context = staticmethod(context)
    history_context = staticmethod(history_context)
    relevant_snapshot_context = staticmethod(relevant_snapshot_context)


webpage_service = WebpageService()
