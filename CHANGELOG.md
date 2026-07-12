# Changelog

All notable changes to vimgym are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — provider-neutral revival

### Added

- Provider-neutral schema v2 with sources, workspaces, sessions, artifacts,
  ordered messages/blocks, block FTS, tools/files, lineage, revision, and health.
- Built-in streaming adapters for current Claude Code roots, subagents, textual
  tool-result sidecars, and Codex active/archived user, automation, and subagent
  JSONL.
- Revision-aware coalesced capture, startup reconciliation, partial-tail
  deferral, active-to-archived identity updates, reindexing, and source health.
- Cursor-paginated provider-neutral APIs/UI, structured text snippets,
  oversized-block loading, Markdown/canonical JSONL streaming export, provider
  badges, lifecycle/lineage, keyboard controls, and bounded transcript DOM.
- Owner-only `.vgbak` create/verify/atomic whole-vault restore with online SQLite
  snapshots, manifests, SHA-256, path-traversal protection, and replacement
  rollback backups.
- `vg doctor --json`, provider/type/lifecycle search filters, and `vg reindex`.
- Deterministic sanitized provider fixtures and a reproducible exact
  100 MiB/25,000-message/500-session benchmark generator.

### Security and privacy

- Redaction is now recursive and enforced before temporary staging, persistence,
  FTS, diagnostics, APIs, WebSockets, export, and backup; invalid policies fail
  closed and policy changes force reindex.
- Provider-native raw JSONL storage and `/raw` were removed.
- Search snippets no longer contain HTML, and the browser uses text-node-only
  provider rendering with local assets.
- Loopback configuration, Host/Origin checks, restrictive CSP, MIME/frame/
  referrer headers, and owner-only vault/backup files are enforced.

### Migration and recovery

- v0.1.1 vaults receive an online rollback snapshot, transactional re-redacted
  migration, canonical row/search rebuild, integrity/foreign-key/count checks,
  deleted-page vacuum, and current-parser reconciliation.
- Portable restore validates all bytes and SQLite state before destination
  mutation; restored history works when original provider roots are absent.

### Release integrity

- Python 3.11–3.14 Ubuntu/macOS, security/browser, installed-wheel, migration,
  backup, coverage, dependency, audit, benchmark, and cross-platform restore
  lanes.
- Release artifacts are built once, hashed, manifested, attested, and published
  as identical PyPI/GitHub bytes without skip-existing or clobber recovery.
- The historic v0.1.1 mismatch is documented: PyPI artifacts came from
  `456bf81`, while the tag later pointed to `bcb4442`. The tag is not moved again.

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
- **Zsh shell completion** (`completions/_vg`) was added to the source
  distribution. The v0.1.1 Homebrew formula did not install it automatically;
  this historical packaging claim is corrected here.
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

[0.2.0]: https://github.com/shoaibrain/vimgym/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/shoaibrain/vimgym/releases/tag/v0.1.1
