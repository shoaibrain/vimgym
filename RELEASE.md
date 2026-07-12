# Vimgym immutable release runbook

This runbook applies to v0.2.0 and later. Published tags and bytes are never
moved, overwritten, clobbered, or rebuilt.

## One-time repository setup

Before the first v0.2 release:

- Protect `v*` tags with a GitHub ruleset that restricts creation, update, and
  deletion to release operators. The workflow separately rejects lightweight
  or non-semantic tags.
- Register both `release.yml` and `release-resume.yml`, using the `pypi`
  environment, as PyPI Trusted Publisher identities.
- Configure `HOMEBREW_TAP_TOKEN` with the `public_repo` and `workflow` scopes
  needed to open a tap PR and observe its checks.

## Required evidence before tagging

The exact commit must be on protected `main` and have successful required checks:

- Ruff, MyPy, ShellCheck, fixture leak scan, package-data/build validation.
- Python 3.11–3.14 on Ubuntu and macOS.
- Overall 85% line/75% branch coverage and critical-module 90% line/85%
  branch coverage.
- Chromium stored-XSS/CSP/Host/Origin/accessibility tests.
- v1 migration, revision/restart/partial-tail, Claude/Codex mappings, active to
  archived, redaction sentinel egress, backup corruption/traversal/restore.
- Installed-wheel smoke on Ubuntu and macOS.
- A green manual/nightly release-candidate run with the deterministic
  100 MiB/25,000-message benchmark, dependency resolutions, `pip-audit`, long
  watcher reconciliation, and cross-platform backup restore.

There is no waiver for data loss, secret leakage, stored XSS, public exposure,
migration recovery failure, backup corruption, or artifact mismatch.

Dispatch **nightly and release-candidate evidence** on the intended `main`
commit and wait for it to succeed before creating the tag. The release workflow
queries both `ci.yml` and `nightly.yml` and refuses a tag whose exact commit has
no successful completed run of either workflow.

## Prepare the version

Update all of the following on one reviewed commit:

- `pyproject.toml` project version.
- `src/vimgym/__init__.py` version.
- `CHANGELOG.md` release heading and comparison link.
- README/guide/API compatibility statements.

Run locally:

```bash
python -m pytest
ruff check src tests
mypy src/vimgym
python -m build
twine check dist/*
```

Delete local `dist/` afterward; the release workflow is the build authority.

## Create a protected annotated tag

Only after the commit's required checks and release-candidate workflow pass:

```bash
git switch main
git pull --ff-only
git tag -a v0.2.0 -m "v0.2.0"
git push origin v0.2.0
```

The `immutable release` workflow verifies that the ref is an annotated tag, the
tag commit has required checks, every version surface matches, and the changelog
contains the version.

## Build and publication flow

The workflow:

1. Builds wheel and sdist exactly once.
2. Runs `twine check`.
3. Creates `SHA256SUMS` and `release-manifest.json` tying tag, commit, names,
   sizes, and SHA-256 values together.
4. Produces GitHub artifact attestations.
5. Stores the build-once directory as `release-dist-TAG` for 90 days.
6. Downloads and re-verifies that artifact in the publish job.
7. Creates a draft GitHub Release and uploads assets without `--clobber`.
8. Verifies GitHub asset digests against `SHA256SUMS`.
9. Publishes the same wheel/sdist directory to PyPI through Trusted Publishing.
10. Verifies PyPI SHA-256 values against `SHA256SUMS`.
11. Publishes the GitHub draft.
12. Opens a Homebrew tap PR using the verified PyPI sdist/tag/commit.

The release is complete only when PyPI, GitHub assets/attestations, and the
Homebrew PR's macOS 14/15 source-install tests are green.

## Partial-publication recovery

Never rerun the normal release workflow after any destination accepted bytes.
Never add `skip-existing` or `--clobber`.

Use **Actions → resume immutable release** with:

- the original tag; and
- the failed release workflow run ID containing `release-dist-TAG`.

The recovery workflow downloads the original build-once artifact, verifies its
hashes and tag/commit manifest, verifies any already-published PyPI bytes match,
uploads only absent GitHub assets, publishes only original distribution files
that are absent from PyPI, completes the draft, and opens or resumes the
Homebrew formula PR. It never invokes a build backend.

If the retained workflow artifact is unavailable, stop and publish a new patch
version. Do not reconstruct supposedly identical bytes.

## Broken release

Published bytes remain immutable:

1. Yank the PyPI version with a reason.
2. Mark the GitHub Release affected and link the incident.
3. Revert the Homebrew formula through a PR.
4. Fix on `main`, pass all gates, and publish a new patch version.

Do not delete/recreate the release, move the tag, replace assets, or reuse the
version number.

## Historic v0.1.1 provenance anomaly

The PyPI v0.1.1 artifacts were built from commit `456bf81`, while the live
`v0.1.1` tag later pointed to `bcb4442`; GitHub/PyPI bytes also differed because
the old workflow combined PyPI `skip-existing` with GitHub `--clobber` rebuilds.
This history is documented rather than “fixed” by moving the tag again. v0.2's
build-once manifest, digest verification, and recovery workflow exist to prevent
that class of provenance break.
