"""Optional macOS LaunchAgent for keeping the local-only backend available."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import config
from runtime_paths import DATA_DIR


LABEL = "local.jtyhome.companion"


class LocalServiceManager:
    @staticmethod
    def supported() -> bool:
        return sys.platform == "darwin"

    @staticmethod
    def plist_path() -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @staticmethod
    def _domain() -> str:
        return f"gui/{os.getuid()}"

    def status(self) -> dict[str, Any]:
        path = self.plist_path()
        loaded = False
        detail = ""
        if self.supported():
            result = subprocess.run(
                ["/bin/launchctl", "print", f"{self._domain()}/{LABEL}"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            loaded = result.returncode == 0
            if not loaded:
                detail = (result.stderr or result.stdout or "尚未加载")[-500:]
        else:
            detail = "本机常驻安装只适用于 macOS；当前系统仍可手动启动"
        return {
            "supported": self.supported(),
            "installed": path.exists(),
            "loaded": loaded,
            "label": LABEL,
            "plist": str(path),
            "detail": detail.strip(),
            "sleep_note": "Mac 完全休眠或关机时本地模型后台不会运行；唤醒后 LaunchAgent 会自动恢复。",
        }

    def install(self) -> dict[str, Any]:
        if not self.supported():
            raise ValueError("本机常驻只支持 macOS")
        project = Path(config.BASE_DIR).resolve()
        python = Path(sys.executable).resolve()
        logs = Path(DATA_DIR) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = self.plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": LABEL,
            "ProgramArguments": [
                str(python), "-m", "uvicorn", "app:app",
                "--host", "127.0.0.1", "--port", "5000",
            ],
            "WorkingDirectory": str(project),
            "EnvironmentVariables": {
                "JTYHOME_DATA_DIR": str(Path(DATA_DIR).resolve()),
                "JTYHOME_ENV_FILE": str(Path(config._ENV_FILE_PATH).resolve()),
                "PYTHONUNBUFFERED": "1",
            },
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / "local-service.log"),
            "StandardErrorPath": str(logs / "local-service-error.log"),
        }
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as target:
            plistlib.dump(payload, target, sort_keys=True)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        subprocess.run(
            ["/bin/launchctl", "bootout", self._domain(), str(path)],
            capture_output=True, text=True, timeout=8, check=False,
        )
        loaded = subprocess.run(
            ["/bin/launchctl", "bootstrap", self._domain(), str(path)],
            capture_output=True, text=True, timeout=8, check=False,
        )
        result = self.status()
        if loaded.returncode != 0 and not result.get("loaded"):
            raise RuntimeError((loaded.stderr or loaded.stdout or "LaunchAgent 加载失败")[-800:])
        return result

    def uninstall(self) -> dict[str, Any]:
        if not self.supported():
            raise ValueError("本机常驻只支持 macOS")
        path = self.plist_path()
        subprocess.run(
            ["/bin/launchctl", "bootout", self._domain(), str(path)],
            capture_output=True, text=True, timeout=8, check=False,
        )
        path.unlink(missing_ok=True)
        return self.status()

    def health(self) -> dict[str, Any]:
        status = self.status()
        state = "已加载" if status.get("loaded") else (
            "已安装，等待下次启动" if status.get("installed") else "未安装（可选）"
        )
        return {"health": "ok", "detail": state}


local_service = LocalServiceManager()

