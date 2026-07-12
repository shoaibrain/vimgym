"""Tests for `vg doctor`."""

import json
import sqlite3
import subprocess
import sys


def run(args, env_vault):
    return subprocess.run(
        [sys.executable, "-m", "vimgym.cli"] + args,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(env_vault.parent),
            "VIMGYM_PATH": str(env_vault),
        },
    )


def test_doctor_runs_on_uninitialized_vault(tmp_path):
    """Doctor must work on a fresh, uninitialized vault and exit cleanly."""
    vault = tmp_path / "vault"
    r = run(["doctor"], vault)
    # Uninitialized vault is a warning, not an error.
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "vimgym doctor" in r.stdout
    assert "Python" in r.stdout
    assert "SQLite" in r.stdout
    assert "redaction" in r.stdout


def test_doctor_runs_on_initialized_vault(tmp_path):
    """After `vg init`, doctor reports the vault healthy."""
    vault = tmp_path / "vault"
    init = run(["init"], vault)
    assert init.returncode == 0

    r = run(["doctor"], vault)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "vault dir" in r.stdout
    # Either a daemon-not-running warning or a daemon-running line
    assert "daemon" in r.stdout
    assert "redaction" in r.stdout
    # The bundled defaults must always load -- this catches the wheel-missing
    # defaults bug.
    assert "patterns loaded" in r.stdout


def test_doctor_json_rejects_future_schema_read_only(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    db_path = vault / "vault.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version=99")
    conn.execute("CREATE TABLE future_data(value TEXT)")
    conn.execute("INSERT INTO future_data VALUES('preserve me')")
    conn.commit()
    conn.close()

    result = run(["doctor", "--json"], vault)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["vault"]["schema_version"] == 99
    assert any(issue["code"] == "future_schema" for issue in report["issues"])
    assert not db_path.with_name("vault.db-wal").exists()
    assert not db_path.with_name("vault.db-shm").exists()


def test_doctor_json_reports_disabled_missing_restored_source(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "vault_dir": str(vault),
                "sources": [
                    {
                        "id": "codex_active",
                        "name": "Codex active sessions",
                        "type": "codex",
                        "path": str(tmp_path / "missing-codex"),
                        "enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run(["doctor", "--json"], vault)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    source = report["sources"][0]
    assert source["enabled"] is False
    assert source["exists"] is False
    assert any(issue["code"] == "source_missing" for issue in report["issues"])
