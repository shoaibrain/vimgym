"""Tests for CLI flags on `vg start`."""
import subprocess
import sys


def run(args):
    return subprocess.run(
        [sys.executable, "-m", "vimgym.cli"] + args,
        capture_output=True, text=True,
    )


def test_start_help_lists_no_browser():
    r = run(["start", "--help"])
    assert r.returncode == 0
    assert "--no-browser" in r.stdout


def test_doctor_help_works():
    r = run(["doctor", "--help"])
    assert r.returncode == 0
    assert "diagnostic" in r.stdout.lower() or "doctor" in r.stdout.lower()


def test_top_level_help_lists_doctor():
    r = run(["--help"])
    assert r.returncode == 0
    assert "doctor" in r.stdout
