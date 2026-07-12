"""Portable, verified Vimgym vault backups.

The portable backup is intentionally a whole-vault operation.  It contains a
consistent SQLite snapshot and the small pieces of configuration required to
open that snapshot on another machine; provider source files, logs, PID files,
and SQLite sidecars are never copied.

Backup files are still sensitive.  Credential redaction reduces accidental
secret retention, but it is not anonymisation or encryption.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from vimgym import __version__


BACKUP_FORMAT = "vimgym-backup"
BACKUP_FORMAT_VERSION = 2
SUPPORTED_SCHEMA_VERSION = 2
BACKUP_SUFFIX = ".vgbak"

_REQUIRED_PAYLOADS = frozenset({"vault.db", "config.json"})
_OPTIONAL_PAYLOADS = frozenset({"redaction-rules.json"})
_ALLOWED_MEMBERS = frozenset({"manifest.json"}) | _REQUIRED_PAYLOADS | _OPTIONAL_PAYLOADS
_COUNTED_TABLES = (
    "sources",
    "workspaces",
    "sessions",
    "source_artifacts",
    "messages",
    "message_blocks",
    "session_tools",
    "session_files",
    # v0.1 compatibility.  These tables are useful when creating the rollback
    # backup immediately before an in-place v1 -> v2 migration.
    "projects",
    "sessions_raw",
)
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


class BackupError(RuntimeError):
    """A backup could not be safely created, verified, or restored."""


@dataclass(frozen=True)
class BackupResult:
    """Result returned by :func:`create_backup` and :func:`verify_backup`."""

    path: Path
    manifest: Mapping[str, Any]

    @property
    def schema_version(self) -> int:
        return int(self.manifest["schema_version"])

    @property
    def counts(self) -> Mapping[str, int]:
        return self.manifest["counts"]


@dataclass(frozen=True)
class RestoreResult:
    """A completed whole-vault restore."""

    vault_dir: Path
    manifest: Mapping[str, Any]
    rollback_backup: Path | None
    missing_sources: tuple[str, ...]


def create_backup(
    vault_dir: Path | str,
    destination: Path | str,
    *,
    now: datetime | None = None,
    app_version: str = __version__,
) -> BackupResult:
    """Create and verify an owner-only portable backup.

    ``destination`` may be an explicit ``.vgbak`` filename or a directory.  A
    directory destination receives the canonical timestamped filename.  The
    SQLite online-backup API is used, so callers may safely create a backup
    while the daemon is reading and writing the source vault.
    """

    source_dir = Path(vault_dir).expanduser()
    db_path = source_dir / "vault.db"
    config_path = source_dir / "config.json"
    rules_path = source_dir / "redaction-rules.json"

    if not db_path.is_file():
        raise BackupError(f"vault database does not exist: {db_path}")
    if not config_path.is_file():
        raise BackupError(f"vault configuration does not exist: {config_path}")

    created_at = _normalise_now(now)
    archive_path = _backup_destination(Path(destination).expanduser(), created_at)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        raise BackupError(f"backup destination already exists: {archive_path}")

    work_dir = Path(tempfile.mkdtemp(prefix=f".{archive_path.name}.work-", dir=archive_path.parent))
    os.chmod(work_dir, 0o700)
    # The work directory is a unique sibling of the final archive, so this
    # temporary file cannot collide with another backup in the same process
    # while still allowing an atomic same-filesystem rename.
    temp_archive = work_dir / f".{archive_path.name}.tmp"
    published = False

    try:
        snapshot_path = work_dir / "vault.db"
        _online_sqlite_backup(db_path, snapshot_path)
        schema_version, counts = _validate_sqlite(snapshot_path)

        _copy_owner_only(config_path, work_dir / "config.json")
        payload_paths: dict[str, Path] = {
            "vault.db": snapshot_path,
            "config.json": work_dir / "config.json",
        }
        if rules_path.is_file():
            _copy_owner_only(rules_path, work_dir / "redaction-rules.json")
            payload_paths["redaction-rules.json"] = work_dir / "redaction-rules.json"

        members = {
            name: {"size": path.stat().st_size, "sha256": _sha256_file(path)}
            for name, path in sorted(payload_paths.items())
        }
        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "vimgym_version": app_version,
            "schema_version": schema_version,
            "counts": counts,
            "members": members,
        }
        manifest_path = work_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)

        _write_zip(temp_archive, manifest_path, payload_paths)
        _fsync_file(temp_archive)
        if archive_path.exists():
            raise BackupError(f"backup destination appeared during creation: {archive_path}")
        os.replace(temp_archive, archive_path)
        published = True
        os.chmod(archive_path, 0o600)
        _fsync_file(archive_path)
        _fsync_directory(archive_path.parent)

        # Verify the bytes at their final path before reporting success.
        return verify_backup(archive_path)
    except BackupError:
        if published:
            archive_path.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        if published:
            archive_path.unlink(missing_ok=True)
        raise BackupError(f"failed to create backup: {exc}") from exc
    finally:
        try:
            temp_archive.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)


def verify_backup(
    archive: Path | str,
    *,
    max_schema_version: int = SUPPORTED_SCHEMA_VERSION,
) -> BackupResult:
    """Verify structure, payload hashes, JSON, SQLite integrity, and counts."""

    archive_path = Path(archive).expanduser()
    if not archive_path.is_file():
        raise BackupError(f"backup archive does not exist: {archive_path}")

    temp_dir = Path(tempfile.mkdtemp(prefix=".vimgym-verify-"))
    os.chmod(temp_dir, 0o700)
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            entries = _validated_entries(zf)
            manifest = _read_manifest(zf, entries["manifest.json"])
            _validate_manifest(manifest, entries)

            declared_members: Mapping[str, Mapping[str, Any]] = manifest["members"]
            for name, expected in declared_members.items():
                actual_size, actual_hash = _hash_zip_member(zf, entries[name])
                if actual_size != expected["size"]:
                    raise BackupError(
                        f"member size mismatch for {name}: "
                        f"expected {expected['size']}, got {actual_size}"
                    )
                if actual_hash != expected["sha256"]:
                    raise BackupError(f"member checksum mismatch for {name}")

            _validate_json_member(zf, entries["config.json"], "config.json", require_object=True)
            if "redaction-rules.json" in entries:
                _validate_json_member(
                    zf,
                    entries["redaction-rules.json"],
                    "redaction-rules.json",
                    require_object=False,
                )

            snapshot_path = temp_dir / "vault.db"
            _copy_zip_member(zf, entries["vault.db"], snapshot_path)

        schema_version, counts = _validate_sqlite(
            snapshot_path,
            max_schema_version=max_schema_version,
        )
        if schema_version != manifest["schema_version"]:
            raise BackupError(
                "manifest schema version does not match the SQLite snapshot "
                f"({manifest['schema_version']} != {schema_version})"
            )
        if counts != manifest["counts"]:
            raise BackupError(
                f"manifest row counts do not match the SQLite snapshot "
                f"({manifest['counts']} != {counts})"
            )
        return BackupResult(path=archive_path, manifest=manifest)
    except BackupError:
        raise
    except zipfile.BadZipFile as exc:
        raise BackupError(f"invalid ZIP backup: {exc}") from exc
    except (OSError, sqlite3.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupError(f"backup verification failed: {exc}") from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def restore_backup(
    archive: Path | str,
    destination: Path | str,
    *,
    replace: bool = False,
    is_daemon_running: Callable[[Path], bool] | None = None,
) -> RestoreResult:
    """Verify and atomically restore an archive as a whole vault.

    ``is_daemon_running`` is an integration hook for the CLI/daemon module.  It
    receives the destination vault directory and must return ``True`` when a
    daemon is active.  Without a hook, the conservative PID-file check in this
    module is used.

    A non-empty destination requires ``replace=True``.  Before replacement, a
    portable rollback backup of the current vault is created in the
    destination's parent directory.
    """

    archive_path = Path(archive).expanduser()
    target = Path(destination).expanduser().absolute()
    if target == target.parent:
        raise BackupError("refusing to restore over a filesystem root")
    if target.is_symlink():
        raise BackupError(f"restore destination must not be a symbolic link: {target}")
    if target.exists() and not target.is_dir():
        raise BackupError(f"restore destination is not a directory: {target}")

    # Verification must finish before any destination state is changed.
    verified = verify_backup(archive_path)

    running_check = is_daemon_running or _pid_file_reports_running
    if running_check(target):
        raise BackupError(f"Vimgym daemon is running for destination: {target}")

    target_nonempty = target.is_dir() and any(target.iterdir())
    if target_nonempty and not replace:
        raise BackupError("restore destination is not empty; pass replace=True")

    rollback_backup: Path | None = None
    if target_nonempty:
        rollback_destination = _unused_backup_path(target.parent, _normalise_now(None))
        rollback_backup = create_backup(target, rollback_destination).path

    target.parent.mkdir(parents=True, exist_ok=True)
    restore_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    os.chmod(restore_dir, 0o700)
    old_dir = target.parent / f".{target.name}.replaced-{os.getpid()}"
    moved_old = False
    installed = False
    missing_sources: tuple[str, ...] = ()

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            entries = _validated_entries(zf)
            # verify_backup already authenticated these payloads.  Re-read them
            # without extractall so archive paths never control filesystem paths.
            expected_members: Mapping[str, Mapping[str, Any]] = verified.manifest["members"]
            _copy_verified_zip_member(
                zf,
                entries["vault.db"],
                restore_dir / "vault.db",
                expected_members["vault.db"],
            )
            _copy_verified_zip_member(
                zf,
                entries["config.json"],
                restore_dir / "config.json",
                expected_members["config.json"],
            )
            if "redaction-rules.json" in entries:
                _copy_verified_zip_member(
                    zf,
                    entries["redaction-rules.json"],
                    restore_dir / "redaction-rules.json",
                    expected_members["redaction-rules.json"],
                )

        missing_sources = _make_config_portable(restore_dir / "config.json", target)
        for restored_file in restore_dir.iterdir():
            os.chmod(restored_file, 0o600)
            _fsync_file(restored_file)
        _fsync_directory(restore_dir)

        if old_dir.exists():
            raise BackupError(f"temporary replacement path already exists: {old_dir}")

        if target.exists():
            if any(target.iterdir()):
                os.replace(target, old_dir)
                moved_old = True
            else:
                target.rmdir()

        try:
            os.replace(restore_dir, target)
            installed = True
            _fsync_directory(target.parent)
        except Exception:
            if moved_old and not target.exists() and old_dir.exists():
                os.replace(old_dir, target)
                moved_old = False
                _fsync_directory(target.parent)
            raise

        if moved_old:
            shutil.rmtree(old_dir)
            moved_old = False
            _fsync_directory(target.parent)

        return RestoreResult(
            vault_dir=target,
            manifest=verified.manifest,
            rollback_backup=rollback_backup,
            missing_sources=missing_sources,
        )
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise BackupError(f"failed to restore backup: {exc}") from exc
    finally:
        if not installed:
            shutil.rmtree(restore_dir, ignore_errors=True)
        if moved_old and old_dir.exists() and not target.exists():
            try:
                os.replace(old_dir, target)
            except OSError:
                pass


def _normalise_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0)


def _backup_destination(destination: Path, created_at: datetime) -> Path:
    if destination.suffix.lower() == BACKUP_SUFFIX:
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    return destination / _backup_filename(created_at)


def _backup_filename(created_at: datetime) -> str:
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"vimgym-{stamp}-v{BACKUP_FORMAT_VERSION}{BACKUP_SUFFIX}"


def _unused_backup_path(directory: Path, created_at: datetime) -> Path:
    candidate_time = created_at
    for _ in range(24 * 60 * 60):
        candidate = directory / _backup_filename(candidate_time)
        if not candidate.exists():
            return candidate
        candidate_time += timedelta(seconds=1)
    raise BackupError(f"could not choose an unused rollback backup name in {directory}")


def _online_sqlite_backup(source_path: Path, destination_path: Path) -> None:
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination = sqlite3.connect(destination_path)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=256, sleep=0.01)
        destination.commit()
    finally:
        destination.close()
        source.close()
    os.chmod(destination_path, 0o600)
    _fsync_file(destination_path)


def _validate_sqlite(
    db_path: Path,
    *,
    max_schema_version: int = SUPPORTED_SCHEMA_VERSION,
) -> tuple[int, dict[str, int]]:
    try:
        db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open SQLite snapshot: {exc}") from exc
    try:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        if integrity_rows != [("ok",)]:
            detail = "; ".join(str(row[0]) for row in integrity_rows[:10])
            raise BackupError(f"SQLite integrity_check failed: {detail}")

        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise BackupError(
                f"SQLite foreign_key_check failed with {len(foreign_key_rows)} violation(s)"
            )

        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if schema_version == 0:
            schema_version = _legacy_schema_version(conn)
        if schema_version < 1 or schema_version > max_schema_version:
            raise BackupError(
                f"unsupported vault schema version {schema_version}; "
                f"supported versions are 1..{max_schema_version}"
            )

        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in _COUNTED_TABLES
            if table in existing
        }
        return schema_version, counts
    except sqlite3.Error as exc:
        raise BackupError(f"invalid SQLite snapshot: {exc}") from exc
    finally:
        conn.close()


def _legacy_schema_version(conn: sqlite3.Connection) -> int:
    config_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='config'"
    ).fetchone()
    if not config_exists:
        return 0
    row = conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _write_zip(
    archive_path: Path,
    manifest_path: Path,
    payload_paths: Mapping[str, Path],
) -> None:
    fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zf:
            _write_zip_file(zf, "manifest.json", manifest_path)
            for name, path in sorted(payload_paths.items()):
                _write_zip_file(zf, name, path)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _write_zip_file(zf: zipfile.ZipFile, name: str, path: Path) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname=name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    with path.open("rb") as source, zf.open(info, "w") as target:
        shutil.copyfileobj(source, target, length=_COPY_CHUNK_BYTES)


def _validated_entries(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        name = info.filename
        if name in entries:
            raise BackupError(f"duplicate archive member: {name}")
        _validate_member_name(name)
        if name not in _ALLOWED_MEMBERS:
            raise BackupError(f"unexpected archive member: {name}")
        if info.flag_bits & 0x1:
            raise BackupError(f"encrypted archive members are not supported: {name}")
        unix_mode = info.external_attr >> 16
        if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
            raise BackupError(f"symbolic-link archive member is not allowed: {name}")
        if info.is_dir():
            raise BackupError(f"directory archive member is not allowed: {name}")
        entries[name] = info

    missing = ({"manifest.json"} | _REQUIRED_PAYLOADS) - entries.keys()
    if missing:
        raise BackupError(f"archive is missing required member(s): {', '.join(sorted(missing))}")
    return entries


def _validate_member_name(name: str) -> None:
    if not name or "\\" in name:
        raise BackupError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError(f"unsafe archive member path: {name!r}")
    if len(path.parts) != 1:
        raise BackupError(f"nested archive member path is not allowed: {name!r}")


def _read_manifest(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, Any]:
    if info.file_size > _MAX_MANIFEST_BYTES:
        raise BackupError("manifest.json exceeds the 1 MiB safety limit")
    raw = zf.read(info)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise BackupError("manifest.json must contain a JSON object")
    return value


def _validate_manifest(
    manifest: Mapping[str, Any],
    entries: Mapping[str, zipfile.ZipInfo],
) -> None:
    required_keys = {
        "format",
        "format_version",
        "created_at",
        "vimgym_version",
        "schema_version",
        "counts",
        "members",
    }
    missing_keys = required_keys - manifest.keys()
    if missing_keys:
        raise BackupError(f"manifest is missing field(s): {', '.join(sorted(missing_keys))}")
    if manifest["format"] != BACKUP_FORMAT:
        raise BackupError(f"unsupported backup format: {manifest['format']!r}")
    if manifest["format_version"] != BACKUP_FORMAT_VERSION:
        raise BackupError(f"unsupported backup format version: {manifest['format_version']!r}")
    if type(manifest["schema_version"]) is not int:
        raise BackupError("manifest schema_version must be an integer")
    if not isinstance(manifest["vimgym_version"], str) or not manifest["vimgym_version"]:
        raise BackupError("manifest vimgym_version must be a non-empty string")
    if not isinstance(manifest["created_at"], str):
        raise BackupError("manifest created_at must be a string")
    try:
        datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("manifest created_at is not a valid ISO timestamp") from exc

    counts = manifest["counts"]
    if not isinstance(counts, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in counts.items()
    ):
        raise BackupError("manifest counts must map table names to non-negative integers")

    members = manifest["members"]
    if not isinstance(members, dict):
        raise BackupError("manifest members must be an object")
    declared = set(members)
    if not _REQUIRED_PAYLOADS.issubset(declared):
        raise BackupError("manifest does not declare every required payload")
    if not declared.issubset(_REQUIRED_PAYLOADS | _OPTIONAL_PAYLOADS):
        raise BackupError("manifest declares an unsupported payload")
    if declared != set(entries) - {"manifest.json"}:
        raise BackupError("manifest member list does not match archive payloads")

    for name, metadata in members.items():
        if not isinstance(metadata, dict):
            raise BackupError(f"manifest metadata for {name} must be an object")
        if set(metadata) != {"size", "sha256"}:
            raise BackupError(f"manifest metadata for {name} must contain size and sha256")
        size = metadata["size"]
        digest = metadata["sha256"]
        if type(size) is not int or size < 0:
            raise BackupError(f"manifest size for {name} is invalid")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise BackupError(f"manifest SHA-256 for {name} is invalid")


def _hash_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with zf.open(info, "r") as stream:
        for chunk in iter(lambda: stream.read(_COPY_CHUNK_BYTES), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _validate_json_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    name: str,
    *,
    require_object: bool,
) -> None:
    with zf.open(info, "r") as stream:
        value = json.load(stream)
    if require_object and not isinstance(value, dict):
        raise BackupError(f"{name} must contain a JSON object")


def _copy_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as target, zf.open(info, "r") as source:
            fd = -1
            shutil.copyfileobj(source, target, length=_COPY_CHUNK_BYTES)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _copy_verified_zip_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    expected: Mapping[str, Any],
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as target, zf.open(info, "r") as source:
            fd = -1
            for chunk in iter(lambda: source.read(_COPY_CHUNK_BYTES), b""):
                size += len(chunk)
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
        destination.unlink(missing_ok=True)
        raise BackupError(f"archive payload changed during restore: {info.filename}")


def _make_config_portable(config_path: Path, target: Path) -> tuple[str, ...]:
    value = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BackupError("config.json must contain a JSON object")
    value["vault_dir"] = str(target)

    missing: list[str] = []
    sources = value.get("sources", [])
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            raw_path = source.get("path")
            if not isinstance(raw_path, str):
                continue
            if not Path(raw_path).expanduser().exists():
                source["enabled"] = False
                source_id = source.get("id")
                missing.append(str(source_id) if source_id else raw_path)

    config_path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    return tuple(missing)


def _pid_file_reports_running(vault_dir: Path) -> bool:
    pid_path = vault_dir / "vimgym.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A live process that we cannot signal is still unsafe to replace.
        return True
    return True


def _copy_owner_only(source: Path, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as input_file:
            fd = -1
            shutil.copyfileobj(input_file, target, length=_COPY_CHUNK_BYTES)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some supported filesystems do not expose directory fsync.
        pass
    finally:
        os.close(fd)
