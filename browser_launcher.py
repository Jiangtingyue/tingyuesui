"""Cross-platform browser startup for the local JTYHome server."""
from __future__ import annotations

import os
import platform
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Mapping


_launch_lock = threading.Lock()
_launched_urls: set[str] = set()


def local_browser_url(host: str, port: int) -> str:
    """Return a browser-safe loopback URL for a possibly wildcard bind host."""
    clean_host = str(host or "").strip().strip("[]")
    if clean_host == "::":
        clean_host = "::1"
    elif clean_host in {"", "0.0.0.0", "*"}:
        clean_host = "127.0.0.1"
    if ":" in clean_host:
        clean_host = f"[{clean_host}]"
    return f"http://{clean_host}:{int(port)}/"


def _path_command(path: str | Path, url: str, exists: Callable[[str], bool]) -> list[str] | None:
    candidate = os.path.abspath(os.path.expanduser(str(path)))
    return [candidate, url] if exists(candidate) else None


def chrome_command(
    url: str,
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.isfile,
) -> list[str] | None:
    """Resolve Chrome without a shell, covering normal macOS/Windows/Linux installs."""
    current_system = system or platform.system()
    env = dict(os.environ if environ is None else environ)

    configured = str(env.get("DAXIGUA_CHROME_PATH") or "").strip()
    if configured:
        command = _path_command(configured, url, exists)
        if command:
            return command

    if current_system == "Darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        ):
            command = _path_command(path, url, exists)
            if command:
                return command
    elif current_system == "Windows":
        roots = [
            env.get("PROGRAMFILES"),
            env.get("PROGRAMFILES(X86)"),
            env.get("LOCALAPPDATA"),
        ]
        for root in filter(None, roots):
            command = _path_command(
                Path(str(root)) / "Google" / "Chrome" / "Application" / "chrome.exe",
                url,
                exists,
            )
            if command:
                return command

    executable_names = (
        ("chrome.exe", "chrome") if current_system == "Windows" else
        ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
    )
    for name in executable_names:
        resolved = which(name)
        if resolved:
            return [resolved, url]
    return None


def open_url(url: str, preference: str = "chrome") -> tuple[bool, str]:
    """Open one URL, preferring a real Chrome executable and falling back safely."""
    normalized = str(url).strip()
    if not normalized:
        return False, "empty-url"
    with _launch_lock:
        if normalized in _launched_urls:
            return True, "already-opened"
        if preference == "chrome":
            command = chrome_command(normalized)
            if command:
                try:
                    subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                    )
                    _launched_urls.add(normalized)
                    return True, "chrome"
                except OSError:
                    pass
        try:
            opened = bool(webbrowser.open(normalized, new=2, autoraise=True))
        except (webbrowser.Error, OSError):
            opened = False
        if opened:
            _launched_urls.add(normalized)
            return True, "default-browser"
        return False, "browser-unavailable"


def _server_ready(url: str, expected_version: str | None = None) -> bool:
    status_url = f"{url.rstrip('/')}/api/launcher-ready"
    request = urllib.request.Request(
        status_url,
        headers={"User-Agent": "JTYHome-Local-Launcher/8.9.3"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=0.8) as response:
            if not 200 <= int(response.status) < 300:
                return False
            payload = json.loads(response.read(4096).decode("utf-8"))
            if payload.get("name") != "大西瓜":
                return False
            return not expected_version or payload.get("version") == expected_version
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def launch_when_ready(
    url: str,
    *,
    preference: str = "chrome",
    timeout_seconds: float = 45.0,
    expected_version: str | None = None,
) -> threading.Thread:
    """Poll the owned local server in a daemon thread, then open exactly one tab."""
    normalized = str(url).strip()

    def worker() -> None:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            if _server_ready(normalized, expected_version):
                opened, backend = open_url(normalized, preference)
                if opened:
                    print(f"[大西瓜] 已通过 {backend} 打开 {normalized}")
                else:
                    print(f"[大西瓜] 服务已就绪，请手动打开 {normalized}")
                return
            time.sleep(0.25)
        print(f"[大西瓜] 等待浏览器启动超时，请手动打开 {normalized}")

    thread = threading.Thread(
        target=worker,
        name="jtyhome-browser-launcher",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = ["chrome_command", "launch_when_ready", "local_browser_url", "open_url"]
