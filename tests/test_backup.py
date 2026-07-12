from __future__ import annotations

import json
import os
import sqlite3
import stat
import warnings
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vimgym.backup import (
    BACKUP_FORMAT,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)


def _make_vault(
    path: Path,
    *,
    label: str = "source",
    schema_version: int = 2,
    source_path: str | None = None,
) -> Path:
    path.mkdir(parents=True)
    db_path = path / "vault.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id)
            );
            CREATE TABLE message_blocks (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES messages(id),
                text TEXT
            );
            """
        )
        conn.execute(f"PRAGMA user_version={schema_version}")
        conn.execute("INSERT INTO sessions VALUES (?, ?)", (label, f"{label} title"))
        conn.execute("INSERT INTO messages VALUES (?, ?)", (f"{label}-message", label))
        conn.execute(
            "INSERT INTO message_blocks VALUES (?, ?, ?)",
            (f"{label}-block", f"{label}-message", f"{label} text"),
        )
        conn.commit()
    finally:
        conn.close()

    source_root = source_path or str(path / "provider-root")
    config = {
        "schema_version": 2,
        "vault_dir": str(path),
        "sources": [
            {
                "id": "claude",
                "name": "Claude",
                "type": "claude_code",
                "path": source_root,
                "enabled": True,
            }
        ],
    }
    (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (path / "redaction-rules.json").write_text(
        json.dumps({"patterns": [{"name": "token", "pattern": "secret"}]}),
        encoding="utf-8",
    )
    (path / "vimgym.pid").write_text("999999999", encoding="utf-8")
    (path / "logs").mkdir()
    (path / "logs" / "vimgym.log").write_text("excluded", encoding="utf-8")
    (path / "vault.db-wal").write_text("excluded", encoding="utf-8")
    return path


def _read_session_ids(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT id FROM sessions ORDER BY id")]
    finally:
        conn.close()


def _rewrite_zip(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    additions: list[tuple[str, bytes]] | None = None,
) -> None:
    replacements = replacements or {}
    additions = additions or []
    with (
        zipfile.ZipFile(source, "r") as original,
        zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as changed,
    ):
        for info in original.infolist():
            changed.writestr(info.filename, replacements.get(info.filename, original.read(info)))
        for name, value in additions:
            changed.writestr(name, value)


def test_create_and_verify_portable_backup(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    output = tmp_path / "portable.vgbak"

    created = create_backup(
        vault,
        output,
        now=datetime(2026, 7, 12, 12, 34, 56, tzinfo=UTC),
        app_version="0.2.0",
    )
    verified = verify_backup(output)

    assert created.path == output
    assert verified.manifest["format"] == BACKUP_FORMAT
    assert verified.manifest["format_version"] == 2
    assert verified.manifest["created_at"] == "2026-07-12T12:34:56Z"
    assert verified.manifest["vimgym_version"] == "0.2.0"
    assert verified.schema_version == 2
    assert verified.counts == {"sessions": 1, "messages": 1, "message_blocks": 1}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    with zipfile.ZipFile(output) as zf:
        assert set(zf.namelist()) == {
            "manifest.json",
            "vault.db",
            "config.json",
            "redaction-rules.json",
        }
        manifest = json.loads(zf.read("manifest.json"))
        assert set(manifest["members"]) == {
            "vault.db",
            "config.json",
            "redaction-rules.json",
        }
        assert "vimgym.pid" not in zf.namelist()
        assert "vault.db-wal" not in zf.namelist()
        assert not any(name.startswith("logs/") for name in zf.namelist())


def test_directory_destination_uses_canonical_filename(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    output_dir = tmp_path / "backups"

    result = create_backup(
        vault,
        output_dir,
        now=datetime(2026, 7, 12, 1, 2, 3, tzinfo=UTC),
    )

    assert result.path == output_dir / "vimgym-20260712T010203Z-v2.vgbak"
    assert result.path.is_file()


def test_online_backup_includes_committed_wal_rows(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    live = sqlite3.connect(vault / "vault.db")
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO sessions VALUES ('wal', 'visible')")
        live.commit()
        result = create_backup(vault, tmp_path / "online.vgbak")
    finally:
        live.close()

    assert result.counts["sessions"] == 2


def test_backup_requires_database_and_config(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BackupError, match="database does not exist"):
        create_backup(empty, tmp_path / "missing-db.vgbak")

    sqlite3.connect(empty / "vault.db").close()
    with pytest.raises(BackupError, match="configuration does not exist"):
        create_backup(empty, tmp_path / "missing-config.vgbak")


def test_pre_migration_v1_vault_can_be_backed_up(tmp_path: Path) -> None:
    vault = tmp_path / "v1"
    vault.mkdir()
    conn = sqlite3.connect(vault / "vault.db")
    try:
        conn.executescript(
            """
            CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO config VALUES ('schema_version', '1');
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
            INSERT INTO sessions VALUES ('legacy');
            CREATE TABLE sessions_raw (session_uuid TEXT PRIMARY KEY, raw_jsonl TEXT);
            """
        )
        conn.commit()
    finally:
        conn.close()
    (vault / "config.json").write_text('{"vault_dir": "legacy"}', encoding="utf-8")

    result = create_backup(vault, tmp_path / "v1-rollback.vgbak")

    assert result.schema_version == 1
    assert result.counts == {"sessions": 1, "sessions_raw": 0}


def test_verify_rejects_checksum_mismatch(tmp_path: Path) -> None:
    source = create_backup(_make_vault(tmp_path / "vault"), tmp_path / "good.vgbak").path
    bad = tmp_path / "bad-checksum.vgbak"
    _rewrite_zip(source, bad, replacements={"config.json": b'{"changed": true}'})

    with pytest.raises(BackupError, match="mismatch"):
        verify_backup(bad)


@pytest.mark.parametrize("unsafe_name", ["../escape", "/absolute", "..\\escape"])
def test_verify_rejects_path_traversal(tmp_path: Path, unsafe_name: str) -> None:
    source = create_backup(_make_vault(tmp_path / "vault"), tmp_path / "good.vgbak").path
    bad = tmp_path / f"unsafe-{len(unsafe_name)}.vgbak"
    _rewrite_zip(source, bad, additions=[(unsafe_name, b"bad")])

    with pytest.raises(BackupError, match="unsafe archive member path"):
        verify_backup(bad)


def test_verify_rejects_duplicate_and_unexpected_members(tmp_path: Path) -> None:
    source = create_backup(_make_vault(tmp_path / "vault"), tmp_path / "good.vgbak").path
    duplicate = tmp_path / "duplicate.vgbak"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _rewrite_zip(source, duplicate, additions=[("config.json", b"{}")])
    with pytest.raises(BackupError, match="duplicate archive member"):
        verify_backup(duplicate)

    unexpected = tmp_path / "unexpected.vgbak"
    _rewrite_zip(source, unexpected, additions=[("native.jsonl", b"unredacted")])
    with pytest.raises(BackupError, match="unexpected archive member"):
        verify_backup(unexpected)


def test_verify_rejects_future_schema(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault", schema_version=3)
    # A future-schema archive cannot be created by this release, either.
    with pytest.raises(BackupError, match="unsupported vault schema version 3"):
        create_backup(vault, tmp_path / "future.vgbak")


def test_restore_fresh_vault_and_disable_missing_sources(tmp_path: Path) -> None:
    missing_root = tmp_path / "does-not-exist"
    source = _make_vault(tmp_path / "source", source_path=str(missing_root))
    archive = create_backup(source, tmp_path / "portable.vgbak").path
    target = tmp_path / "restored"

    restored = restore_backup(archive, target)

    assert restored.vault_dir == target.absolute()
    assert restored.rollback_backup is None
    assert restored.missing_sources == ("claude",)
    assert _read_session_ids(target / "vault.db") == ["source"]
    config = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert config["vault_dir"] == str(target.absolute())
    assert config["sources"][0]["enabled"] is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in target.iterdir())


def test_restore_requires_replace_and_creates_verified_rollback(tmp_path: Path) -> None:
    source = _make_vault(tmp_path / "source", label="new")
    archive = create_backup(source, tmp_path / "portable.vgbak").path
    target = _make_vault(tmp_path / "target", label="old")

    with pytest.raises(BackupError, match="pass replace=True"):
        restore_backup(archive, target)
    assert _read_session_ids(target / "vault.db") == ["old"]

    restored = restore_backup(archive, target, replace=True)

    assert _read_session_ids(target / "vault.db") == ["new"]
    assert restored.rollback_backup is not None
    rollback = verify_backup(restored.rollback_backup)
    assert rollback.counts["sessions"] == 1
    rollback_target = tmp_path / "rollback-restored"
    restore_backup(restored.rollback_backup, rollback_target)
    assert _read_session_ids(rollback_target / "vault.db") == ["old"]


def test_running_daemon_hook_blocks_restore_without_touching_target(tmp_path: Path) -> None:
    source = _make_vault(tmp_path / "source")
    archive = create_backup(source, tmp_path / "portable.vgbak").path
    target = tmp_path / "target"

    with pytest.raises(BackupError, match="daemon is running"):
        restore_backup(archive, target, is_daemon_running=lambda _: True)

    assert not target.exists()


def test_invalid_archive_never_changes_existing_vault(tmp_path: Path) -> None:
    source = create_backup(_make_vault(tmp_path / "source"), tmp_path / "good.vgbak").path
    invalid = tmp_path / "invalid.vgbak"
    _rewrite_zip(source, invalid, replacements={"vault.db": b"not sqlite"})
    target = _make_vault(tmp_path / "target", label="old")

    with pytest.raises(BackupError):
        restore_backup(invalid, target, replace=True)

    assert _read_session_ids(target / "vault.db") == ["old"]
    assert not list(tmp_path.glob("vimgym-*-v2.vgbak"))


def test_restore_rejects_symlink_destination(tmp_path: Path) -> None:
    source = _make_vault(tmp_path / "source")
    archive = create_backup(source, tmp_path / "portable.vgbak").path
    real = tmp_path / "real"
    real.mkdir()
    symlink = tmp_path / "linked"
    try:
        symlink.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(BackupError, match="symbolic link"):
        restore_backup(archive, symlink)


def test_archive_member_permissions_are_owner_only(tmp_path: Path) -> None:
    archive = create_backup(_make_vault(tmp_path / "vault"), tmp_path / "portable.vgbak").path
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            assert stat.S_IMODE(info.external_attr >> 16) == 0o600


def test_default_pid_check_blocks_live_process(tmp_path: Path) -> None:
    source = _make_vault(tmp_path / "source")
    archive = create_backup(source, tmp_path / "portable.vgbak").path
    target = tmp_path / "target"
    target.mkdir()
    (target / "vimgym.pid").write_text(str(os.getpid()), encoding="utf-8")

    with pytest.raises(BackupError, match="daemon is running"):
        restore_backup(archive, target, replace=True)
