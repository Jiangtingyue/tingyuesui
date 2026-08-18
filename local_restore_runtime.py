"""Validated, restart-time restore for the local JTYHome data directory.

The HTTP process only stages an archive.  Applying it before ``models.init_db``
keeps SQLite, schedulers and chat requests away from files while they are being
replaced.  Credentials and the current pairing token are never part of a
backup and are therefore never overwritten by restore.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


BACKUP_KIND = "jtyhome-local-backup"
BACKUP_SCHEMA = 1
MAX_BACKUP_ENTRIES = 200_000
MAX_RESTORE_BYTES = 64 * 1024**3
PROTECTED_ROOTS = {
    ".jtyhome.env", "access-token.txt", "conversation-imports",
    "diagnostics", "logs", "restore-rollbacks", "last-restore.json",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def pending_paths(data_dir: Path) -> tuple[Path, Path]:
    root = Path(data_dir).resolve()
    return (
        root.parent / f".{root.name}-restore-pending.zip",
        root.parent / f".{root.name}-restore-pending.json",
    )


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(str(name or ""))
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != "data"
    ):
        raise ValueError("备份包包含越界路径")
    return path


def validate_backup(archive_path: Path, *, required_db_name: str) -> dict[str, Any]:
    path = Path(archive_path)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("这不是有效的大西瓜备份包") from exc
    with archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) > MAX_BACKUP_ENTRIES:
            raise ValueError("备份包文件数量超过本机恢复上限")
        if sum(max(0, int(item.file_size)) for item in infos) > MAX_RESTORE_BYTES:
            raise ValueError("备份包解压后超过 64 GB，已停止恢复")
        names = {item.filename for item in infos}
        if len(names) != len(infos):
            raise ValueError("备份包包含重复文件名")
        if "manifest.json" not in names:
            raise ValueError("备份包缺少 manifest.json")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as exc:
            raise ValueError("备份清单无法读取") from exc
        if not isinstance(manifest, dict):
            raise ValueError("备份清单格式无效")
        if manifest.get("kind") != BACKUP_KIND:
            raise ValueError("这不是大西瓜本地数据备份")
        if int(manifest.get("schema") or 0) != BACKUP_SCHEMA:
            raise ValueError("备份版本暂不受当前程序支持")
        entries = manifest.get("entries")
        if not isinstance(entries, dict) or not entries:
            raise ValueError("备份清单没有数据文件")
        if set(entries) != names - {"manifest.json"}:
            raise ValueError("备份清单与压缩包文件列表不一致")
        expected_db = f"data/{required_db_name}"
        if expected_db not in entries or expected_db not in names:
            raise ValueError("备份包缺少主数据库快照")
        for name, expected in entries.items():
            _safe_member(name)
            if name not in names or not isinstance(expected, dict):
                raise ValueError("备份清单与压缩包内容不一致")
            info = archive.getinfo(name)
            try:
                expected_size = int(expected.get("size", -1))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"备份文件大小清单无效：{name}") from exc
            if expected_size != int(info.file_size):
                raise ValueError(f"备份文件大小校验失败：{name}")
            digest = hashlib.sha256()
            with archive.open(info) as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != str(expected.get("sha256") or ""):
                raise ValueError(f"备份文件校验失败：{name}")
        roots = manifest.get("replace_roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError("备份清单缺少恢复范围")
        for root in roots:
            root_path = PurePosixPath(str(root or ""))
            root_name = root_path.parts[0] if root_path.parts else ""
            if (
                len(root_path.parts) != 1
                or root_name in {"", ".", ".."}
                or root_name in PROTECTED_ROOTS
                or root_name.startswith(".")
            ):
                raise ValueError("备份恢复范围无效")
        if required_db_name not in roots:
            raise ValueError("备份清单没有把主数据库列入恢复范围")
        return manifest


def apply_pending_restore(data_dir: Path, required_db_name: str) -> dict[str, Any] | None:
    """Apply a previously staged and fully validated backup, if one exists."""
    data_root = Path(data_dir).resolve()
    archive_path, marker_path = pending_paths(data_root)
    if not archive_path.exists() or not marker_path.exists():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        marker = {}
    work_dir = Path(tempfile.mkdtemp(prefix="jtyhome-restore-", dir=str(data_root.parent)))
    try:
        manifest = validate_backup(archive_path, required_db_name=required_db_name)
        extracted = work_dir / "data"
        extracted.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            for name in manifest["entries"]:
                member = _safe_member(name)
                destination = work_dir.joinpath(*member.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)

        data_root.mkdir(parents=True, exist_ok=True)
        roots = [str(item) for item in manifest["replace_roots"]]
        # Every archive byte has already been extracted and hash-checked.  Only
        # now do we touch current state.  Secret files are absent from roots.
        previous_root = work_dir / "previous"
        previous_root.mkdir(parents=True, exist_ok=True)
        swapped: list[tuple[Path, Path]] = []
        receipt: dict[str, Any] = {}
        try:
            for root_name in roots:
                target = data_root / root_name
                source = extracted / root_name
                previous = previous_root / root_name
                if target.exists() or target.is_symlink():
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, previous)
                swapped.append((target, previous))
                for suffix in ("-wal", "-shm"):
                    sidecar = data_root / f"{root_name}{suffix}"
                    if sidecar.exists() and sidecar.is_file():
                        os.replace(sidecar, previous_root / f"{root_name}{suffix}")
                if source.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)

            applied_at = utcnow()
            receipt = {
                "ok": True,
                "applied_at": applied_at,
                "restored_at": applied_at,
                "backup_created_at": manifest.get("created_at", ""),
                "backup_app_version": manifest.get("app_version", ""),
                "rollback_file": str(marker.get("rollback_file") or ""),
                "credentials_preserved": True,
            }
            receipt_temp = work_dir / "last-restore.json"
            receipt_temp.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(receipt_temp, data_root / "last-restore.json")
        except Exception:
            for target, previous in reversed(swapped):
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists() or target.is_symlink():
                    target.unlink(missing_ok=True)
                if previous.exists() or previous.is_symlink():
                    os.replace(previous, target)
                for suffix in ("-wal", "-shm"):
                    prior_sidecar = previous_root / f"{target.name}{suffix}"
                    if prior_sidecar.exists():
                        os.replace(prior_sidecar, data_root / prior_sidecar.name)
            raise
        archive_path.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
        return receipt
    except Exception as exc:
        failed_archive = archive_path.with_name(
            f"{archive_path.stem}-failed-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        try:
            os.replace(archive_path, failed_archive)
        except OSError:
            failed_archive = archive_path
        failure = {
            "failed_at": utcnow(),
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "archive": str(failed_archive),
            "rollback_file": str(marker.get("rollback_file") or ""),
        }
        marker_path.with_suffix(".failed.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        marker_path.unlink(missing_ok=True)
        return {"error": failure["error"], "failed": True}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
