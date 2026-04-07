"""Tests for `vg doctor`."""
import subprocess
import sys


def run(args, env_vault):
    return subprocess.run(
        [sys.executable, "-m", "vimgym.cli"] + args,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "HOME": str(env_vault.parent),
             "VIMGYM_PATH": str(env_vault)},
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
