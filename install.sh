#!/bin/bash
# vimgym installer — https://vimgym.xyz/install
# usage: curl -fsSL https://vimgym.xyz/install | sh

set -eu

MINIMUM_PYTHON_MAJOR=3
MINIMUM_PYTHON_MINOR=11
INSTALL_METHOD=""

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
if command -v brew >/dev/null 2>&1; then
  INSTALL_METHOD="homebrew"
elif command -v pipx >/dev/null 2>&1; then
  INSTALL_METHOD="pipx"
elif command -v pip3 >/dev/null 2>&1; then
  INSTALL_METHOD="pip"
else
  warn "Error: Neither Homebrew, pipx, nor pip3 found."
  warn "Install Python 3.${MINIMUM_PYTHON_MINOR}+ from https://python.org first."
  exit 1
fi

# ── Python version check (skipped for Homebrew — formula handles it) ─
if [ "$INSTALL_METHOD" != "homebrew" ]; then
  PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
  PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
  PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
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
    brew tap shoaibrain/vimgym 2>/dev/null || true
    brew install vimgym
    ;;
  pipx)
    pipx install vimgym
    ;;
  pip)
    pip3 install --user vimgym
    USER_BIN="$(python3 -m site --user-base)/bin"
    case ":${PATH}:" in
      *":${USER_BIN}:"*) ;;
      *)
        printf '\n'
        printf '%bAdd this to your shell config (~/.zshrc or ~/.bashrc):%b\n' "${C_PINK}" "${C_RESET}"
        printf '  export PATH="%s:$PATH"\n' "${USER_BIN}"
        printf '\n'
        ;;
    esac
    ;;
esac

# ── Initialize vault ──────────────────────────────────────────────────
if command -v vg >/dev/null 2>&1; then
  vg init || true
  printf '\n'
  say "✓ vimgym installed"
  printf '\n'
  printf 'Quick start:\n'
  printf '%b  vg start%b          # start daemon + open browser\n' "${C_DIM}" "${C_RESET}"
  printf '%b  vg search "auth"%b  # search your sessions\n'        "${C_DIM}" "${C_RESET}"
  printf '%b  vg status%b         # check daemon status\n'         "${C_DIM}" "${C_RESET}"
  printf '\n'
  printf 'Docs: https://vimgym.xyz\n'
else
  warn "vg command not found in PATH after install. Add the user bin dir above and retry."
  exit 1
fi
