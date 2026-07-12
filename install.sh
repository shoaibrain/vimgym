#!/bin/bash
# vimgym installer — https://vimgym.xyz/install
# usage: curl -fsSL https://vimgym.xyz/install | sh

set -eu

MINIMUM_PYTHON_MAJOR=3
MINIMUM_PYTHON_MINOR=11
INSTALL_METHOD=""
INSTALL_BIN=""
PYTHON_EXECUTABLE=""
VG_EXECUTABLE=""

# ── Colors (only if stdout is a tty) ─────────────────────────────────
if [ -t 1 ]; then
  C_GREEN='\033[0;32m'
  C_PINK='\033[0;35m'
  C_DIM='\033[2m'
  C_RESET='\033[0m'
else
  C_GREEN=''
  C_PINK=''
  C_DIM=''
  C_RESET=''
fi

say()  { printf '%b%s%b\n' "${C_GREEN}" "$1" "${C_RESET}"; }
warn() { printf '%b%s%b\n' "${C_PINK}" "$1" "${C_RESET}" >&2; }

# ── OS sanity ─────────────────────────────────────────────────────────
if [ "$(uname -s)" != "Darwin" ] && [ "$(uname -s)" != "Linux" ]; then
  warn "Error: vimgym currently supports macOS and Linux only."
  exit 1
fi

# ── Detect install method ─────────────────────────────────────────────
# Order of preference:
#   1. Homebrew  — best UX on macOS, formula handles service & PATH
#   2. pipx      — isolated venv, symlinks vg into a PATH dir, macOS/Linux
#   3. pip --user — last resort, requires PATH fixup
if command -v brew >/dev/null 2>&1; then
  INSTALL_METHOD="homebrew"
elif command -v pipx >/dev/null 2>&1; then
  INSTALL_METHOD="pipx"
elif command -v python3 >/dev/null 2>&1 && python3 -m pip --version >/dev/null 2>&1; then
  INSTALL_METHOD="pip"
else
  warn "Error: Neither Homebrew, pipx, nor a usable Python pip installation was found."
  warn "Install Python 3.${MINIMUM_PYTHON_MINOR}+ from https://python.org first,"
  warn "then run:  pip3 install --user pipx && pipx install vimgym"
  exit 1
fi

# ── Python version check (skipped for Homebrew — formula handles it) ─
if [ "$INSTALL_METHOD" != "homebrew" ]; then
  if ! PYTHON_EXECUTABLE=$(command -v python3); then
    warn "Error: Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR}+ is required."
    warn "Install it from https://python.org and retry."
    exit 1
  fi
  if ! PY_VERSION_OUTPUT=$("$PYTHON_EXECUTABLE" --version 2>&1); then
    warn "Error: Unable to run ${PYTHON_EXECUTABLE} to determine its version."
    exit 1
  fi
  case "$PY_VERSION_OUTPUT" in
    Python\ *) PY_VERSION=${PY_VERSION_OUTPUT#Python } ;;
    *)
      warn "Error: Unable to determine the Python version from: ${PY_VERSION_OUTPUT}"
      exit 1
      ;;
  esac
  PY_MAJOR=${PY_VERSION%%.*}
  PY_REMAINDER=${PY_VERSION#*.}
  PY_MINOR=${PY_REMAINDER%%.*}
  case "$PY_MAJOR" in
    ''|*[!0-9]*)
      warn "Error: Unable to determine the Python version from: ${PY_VERSION_OUTPUT}"
      exit 1
      ;;
  esac
  case "$PY_MINOR" in
    ''|*[!0-9]*)
      warn "Error: Unable to determine the Python version from: ${PY_VERSION_OUTPUT}"
      exit 1
      ;;
  esac
  if [ "$PY_MAJOR" -lt "$MINIMUM_PYTHON_MAJOR" ] || \
     { [ "$PY_MAJOR" -eq "$MINIMUM_PYTHON_MAJOR" ] && [ "$PY_MINOR" -lt "$MINIMUM_PYTHON_MINOR" ]; }; then
    warn "Error: Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR}+ required (found ${PY_VERSION})."
    warn "macOS:  brew install python@3.12"
    warn "Linux:  see https://python.org"
    exit 1
  fi
fi

# ── Install ───────────────────────────────────────────────────────────
say "Installing vimgym via ${INSTALL_METHOD}..."

case "$INSTALL_METHOD" in
  homebrew)
    # Homebrew 6 requires the fully qualified personal-tap formula flow.
    brew install shoaibrain/vimgym/vimgym
    ;;
  pipx)
    # Pin pipx to the interpreter we just validated. --force makes rerunning
    # the installer safe when an older vimgym environment already exists.
    pipx install --force --python "$PYTHON_EXECUTABLE" vimgym
    pipx ensurepath >/dev/null 2>&1 || true
    INSTALL_BIN=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)
    if [ -z "$INSTALL_BIN" ] && [ -n "${HOME:-}" ]; then
      INSTALL_BIN="${HOME}/.local/bin"
    fi
    if [ -n "$INSTALL_BIN" ]; then
      case ":${PATH}:" in
        *":${INSTALL_BIN}:"*) : ;;
        *)
          warn "vg was installed in ${INSTALL_BIN}, which is not in this shell's PATH."
          warn "Open a new terminal before using the quick-start commands below."
          ;;
      esac
    fi
    ;;
  pip)
    # Use the validated interpreter for both installation and path discovery;
    # a standalone pip3 can point at a different (and unsupported) Python.
    "$PYTHON_EXECUTABLE" -m pip install --user --upgrade vimgym
    INSTALL_BIN="$("$PYTHON_EXECUTABLE" -m site --user-base)/bin"
    case ":${PATH}:" in
      *":${INSTALL_BIN}:"*)
        : # already in PATH
        ;;
      *)
        printf '\n'
        warn "⚠  ${INSTALL_BIN} is not in your PATH."
        warn "   vg will not be available until you add it. Add this line"
        warn "   to ~/.zshrc (or ~/.bashrc) and reload your shell:"
        printf '\n'
        # shellcheck disable=SC2016  # the literal $PATH is what the user must paste
        printf '       export PATH="%s:$PATH"\n' "${INSTALL_BIN}"
        printf '\n'
        warn "   We deliberately do NOT edit your shell config automatically."
        warn "   For a hands-off install, use Homebrew or pipx instead."
        printf '\n'
        ;;
    esac
    ;;
esac

# ── Initialize vault & smoke test ─────────────────────────────────────
if [ -n "$INSTALL_BIN" ] && [ -x "${INSTALL_BIN}/vg" ]; then
  # pipx ensurepath and shell-profile edits do not affect the process that is
  # already running. Prefer the binary installed in this run over a stale vg
  # from another installation method that may already be in PATH.
  VG_EXECUTABLE="${INSTALL_BIN}/vg"
elif command -v vg >/dev/null 2>&1; then
  VG_EXECUTABLE=$(command -v vg)
fi

if [ -n "$VG_EXECUTABLE" ]; then
  if ! "$VG_EXECUTABLE" init >/dev/null 2>&1; then
    warn "vimgym installed, but 'vg init' did not complete successfully."
    warn "Run 'vg doctor' for details after fixing the reported environment issue."
  fi
  printf '\n'
  say "✓ vimgym installed"
  printf '\n'
  printf 'Quick start:\n'
  printf '%b  vg doctor%b         # verify install is healthy\n'  "${C_DIM}" "${C_RESET}"
  printf '%b  vg start%b          # start daemon + open browser\n' "${C_DIM}" "${C_RESET}"
  printf '%b  vg search "auth"%b  # search your sessions\n'        "${C_DIM}" "${C_RESET}"
  printf '%b  vg status%b         # check daemon status\n'         "${C_DIM}" "${C_RESET}"
  printf '\n'
  printf 'Docs: https://vimgym.xyz\n'
else
  warn "vg command was not found after the installer completed."
  if [ -n "$INSTALL_BIN" ]; then
    warn "Expected executable: ${INSTALL_BIN}/vg"
  fi
  exit 1
fi
