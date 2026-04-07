# Changelog

All notable changes to vimgym are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — first official release

### Fixed

- **Duplicate log lines.** The daemon's child process attached both a
  `FileHandler` and a `StreamHandler(sys.stderr)`, while the parent's
  `subprocess.Popen` redirected the child's stderr to the same log file.
  Every record was written twice. The child now uses only a
  `RotatingFileHandler`; uvicorn's own loggers are forced to propagate
  through the root logger so we own logging end-to-end.
- **Wheel installs were missing the bundled redaction rules.** The
  `defaults/` directory was referenced via a relative path that only
  worked in editable repo installs. It is now packaged inside
  `vimgym/defaults/` and loaded via `importlib.resources`, so secrets
  are correctly stripped under pip, pipx, and Homebrew installs.
- **Log rotation.** The daemon log now rotates at 5 MB and keeps 5 backups
  (~25 MB total) instead of growing forever.

### Added

- **`vg doctor`** — comprehensive system diagnostic. Reports vimgym version,
  Python version, SQLite + FTS5 availability, vault dir & db permissions,
  daemon state, configured sources, redaction rule count, and free disk
  space. Exits non-zero on any red issue.
- **`vg start --no-browser`** — for use as a background service. The
  Homebrew `brew services` formula uses this so that `launchd` doesn't
  try to pop a browser.
- **Virtualenv self-warning.** `vg start` now warns when invoked from
  a project venv that won't survive a shell restart and points the user
  at `brew install` or `pipx install`.
- **Zsh shell completion** (`completions/_vg`), installed automatically
  by the Homebrew formula.
- **Makefile and `.envrc`** for one-command developer setup.

### Packaging

- First publish to PyPI.
- Homebrew tap at [`shoaibrain/homebrew-vimgym`](https://github.com/shoaibrain/homebrew-vimgym).
- Trusted Publishing (OIDC) for PyPI — no long-lived API tokens in CI.
- GitHub Actions workflow auto-bumps the tap formula on every published
  GitHub Release.
- Cross-platform CI matrix (Linux + macOS, Python 3.11 / 3.12 / 3.13).

### Tests

- Regression test for the duplicate-log-lines bug
  (`tests/test_daemon.py::test_no_duplicate_log_lines`).
- New tests for `vg doctor` and CLI flag parsing.
- Total: 117 tests passing.

[0.1.1]: https://github.com/shoaibrain/vimgym/releases/tag/v0.1.1
