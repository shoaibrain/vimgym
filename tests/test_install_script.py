"""Behavioral installer tests with a fully mocked command PATH.

These tests never invoke Homebrew, pip, pipx, Python, vg, or the network.  Each
executable is a small recorder installed in ``tmp_path/bin`` and the installer
runs with that directory as its entire PATH.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _mock_uname(bin_dir: Path) -> None:
    _executable(bin_dir / "uname", "printf '%s\\n' \"${MOCK_UNAME:-Linux}\"")


def _mock_python(bin_dir: Path) -> Path:
    return _executable(
        bin_dir / "python3",
        r"""
printf 'python3 %s\n' "$*" >> "$MOCK_LOG"
if [ "${1:-}" = "--version" ]; then
  if [ "${MOCK_PYTHON_VERSION_EXIT:-0}" -ne 0 ]; then
    exit "$MOCK_PYTHON_VERSION_EXIT"
  fi
  printf '%s\n' "${MOCK_PYTHON_OUTPUT:-Python 3.12.8}"
  exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
  if [ "${3:-}" = "--version" ]; then
    printf 'pip 25.0 from /mock/site-packages/pip (python 3.12)\n'
  fi
  exit "${MOCK_PIP_EXIT:-0}"
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "site" ] && [ "${3:-}" = "--user-base" ]; then
  printf '%s\n' "$MOCK_USER_BASE"
  exit 0
fi
exit 64
""",
    )


def _mock_pipx(bin_dir: Path) -> None:
    _executable(
        bin_dir / "pipx",
        r"""
printf 'pipx %s\n' "$*" >> "$MOCK_LOG"
if [ "${1:-}" = "environment" ] && [ "${2:-}" = "--value" ] && [ "${3:-}" = "PIPX_BIN_DIR" ]; then
  printf '%s\n' "$MOCK_PIPX_BIN"
fi
exit "${MOCK_PIPX_EXIT:-0}"
""",
    )


def _mock_brew(bin_dir: Path) -> None:
    _executable(
        bin_dir / "brew",
        'printf \'brew %s\\n\' "$*" >> "$MOCK_LOG"\nexit "${MOCK_BREW_EXIT:-0}"',
    )


def _mock_vg(path: Path, label: str = "vg") -> None:
    _executable(
        path,
        f"""
printf '{label} %s\\n' "$*" >> "$MOCK_LOG"
if [ "${{1:-}}" = "init" ]; then
  exit "${{MOCK_VG_INIT_EXIT:-0}}"
fi
exit 0
""",
    )


def _environment(tmp_path: Path, bin_dir: Path, **overrides: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    log = tmp_path / "commands.log"
    log.touch()
    user_base = tmp_path / "user-base"
    pipx_bin = tmp_path / "pipx-bin"
    user_base.mkdir(exist_ok=True)
    pipx_bin.mkdir(exist_ok=True)
    environment = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "MOCK_LOG": str(log),
        "MOCK_UNAME": "Linux",
        "MOCK_PYTHON_OUTPUT": "Python 3.12.8",
        "MOCK_USER_BASE": str(user_base),
        "MOCK_PIPX_BIN": str(pipx_bin),
    }
    environment.update(overrides)
    return environment


def _run(environment: dict[str, str], shell: str = "/bin/bash") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shell, str(INSTALL_SCRIPT)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def _log(environment: dict[str, str]) -> str:
    return Path(environment["MOCK_LOG"]).read_text(encoding="utf-8")


def test_homebrew_wins_and_uses_fully_qualified_formula(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_brew(bin_dir)
    _mock_pipx(bin_dir)
    _mock_python(bin_dir)
    _mock_vg(bin_dir / "vg")
    environment = _environment(tmp_path, bin_dir)

    result = _run(environment)

    assert result.returncode == 0, result.stderr
    log = _log(environment)
    assert "brew install shoaibrain/vimgym/vimgym" in log
    assert "pipx install" not in log
    assert "python3 --version" not in log
    assert "vg init" in log


def test_pipx_is_idempotent_and_verifies_bin_outside_current_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    python = _mock_python(bin_dir)
    _mock_pipx(bin_dir)
    _mock_vg(bin_dir / "vg", label="stale-vg")
    environment = _environment(tmp_path, bin_dir)
    _mock_vg(Path(environment["MOCK_PIPX_BIN"]) / "vg", label="pipx-vg")

    result = _run(environment)

    assert result.returncode == 0, result.stderr
    log = _log(environment)
    assert f"pipx install --force --python {python} vimgym" in log
    assert "pipx ensurepath" in log
    assert "pipx environment --value PIPX_BIN_DIR" in log
    assert "pipx-vg init" in log
    assert "stale-vg init" not in log
    assert "✓ vimgym installed" in result.stdout
    assert "not in this shell's PATH" in result.stderr


def test_pip_fallback_uses_the_validated_interpreter_and_direct_bin(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_python(bin_dir)
    environment = _environment(tmp_path, bin_dir)
    install_bin = Path(environment["MOCK_USER_BASE"]) / "bin"
    _mock_vg(install_bin / "vg")

    result = _run(environment)

    assert result.returncode == 0, result.stderr
    log = _log(environment)
    assert "python3 -m pip --version" in log
    assert "python3 -m pip install --user --upgrade vimgym" in log
    assert "python3 -m site --user-base" in log
    assert "vg init" in log
    assert f"{install_bin} is not in your PATH" in result.stderr
    assert f'export PATH="{install_bin}:$PATH"' in result.stdout


def test_old_python_stops_before_pipx_install(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_python(bin_dir)
    _mock_pipx(bin_dir)
    environment = _environment(tmp_path, bin_dir, MOCK_PYTHON_OUTPUT="Python 3.10.14")

    result = _run(environment)

    assert result.returncode == 1
    assert "Python 3.11+ required (found 3.10.14)" in result.stderr
    assert "pipx install" not in _log(environment)


def test_malformed_python_version_fails_with_a_clear_error(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_python(bin_dir)
    _mock_pipx(bin_dir)
    environment = _environment(tmp_path, bin_dir, MOCK_PYTHON_OUTPUT="unexpected output")

    result = _run(environment)

    assert result.returncode == 1
    assert "Unable to determine the Python version" in result.stderr
    assert "integer expression expected" not in result.stderr
    assert "pipx install" not in _log(environment)


def test_unsupported_os_exits_before_any_installer(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_brew(bin_dir)
    environment = _environment(tmp_path, bin_dir, MOCK_UNAME="Windows_NT")

    result = _run(environment)

    assert result.returncode == 1
    assert "supports macOS and Linux only" in result.stderr
    assert "brew install" not in _log(environment)


def test_init_failure_is_reported_without_misreporting_install_failure(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_brew(bin_dir)
    _mock_vg(bin_dir / "vg")
    environment = _environment(tmp_path, bin_dir, MOCK_VG_INIT_EXIT="7")

    result = _run(environment)

    assert result.returncode == 0
    assert "'vg init' did not complete successfully" in result.stderr
    assert "✓ vimgym installed" in result.stdout


def test_missing_post_install_binary_is_a_hard_failure(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_python(bin_dir)
    _mock_pipx(bin_dir)
    environment = _environment(tmp_path, bin_dir)

    result = _run(environment)

    assert result.returncode == 1
    assert "vg command was not found" in result.stderr
    assert f"Expected executable: {environment['MOCK_PIPX_BIN']}/vg" in result.stderr


def test_mock_path_does_not_inherit_real_installers(tmp_path: Path) -> None:
    """Guard the suite itself: the isolated PATH must contain only test doubles."""

    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    environment = _environment(tmp_path, bin_dir)
    result = _run(environment)
    assert result.returncode == 1
    assert "Neither Homebrew, pipx, nor a usable Python pip" in result.stderr
    assert os.pathsep not in environment["PATH"]


def test_documented_sh_entrypoint_remains_supported(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _mock_uname(bin_dir)
    _mock_brew(bin_dir)
    _mock_vg(bin_dir / "vg")
    environment = _environment(tmp_path, bin_dir)

    result = _run(environment, shell="/bin/sh")

    assert result.returncode == 0, result.stderr
    assert "brew install shoaibrain/vimgym/vimgym" in _log(environment)
