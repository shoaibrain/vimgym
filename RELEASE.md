# Release Runbook

This document covers two distinct things:

1. **One-time setup** — must be done once before the very first release.
2. **Cutting a release** — what to do every time you ship a new version.

After the one-time setup, every subsequent release is essentially:
`bump version → tag → push → publish GitHub Release → merge the auto-generated
tap PR`. The CI does the rest.

---

## Part 1 — One-time setup (do this exactly once, before v0.1.1)

### Step 1.1 — Push the source repo with the new code

This sprint added: `vg doctor`, `vg start --no-browser`, the duplicate-log fix,
the wheel-defaults fix, log rotation, the venv warning, the Makefile, the
zsh completion, the CI workflow, the release workflow, and the v0.1.1
metadata. Push it all to `main`:

```bash
cd ~/vimgym                                # the main source repo
git status                                  # review what changed
git add -A
git commit -m "v0.1.1: doctor, --no-browser, logging fix, wheel-defaults fix, release pipeline"
git push origin main
```

Verify the CI workflow runs and goes green on GitHub:
<https://github.com/shoaibrain/vimgym/actions/workflows/ci.yml>

If CI fails, fix and re-push **before** proceeding.

### Step 1.2 — Push the Homebrew tap repo

The tap repo files are prepared at `~/vimgym/homebrew-vimgym/` (gitignored
inside the main repo's working tree).

```bash
cd ~/vimgym/homebrew-vimgym
git add Formula/vimgym.rb README.md LICENSE .github/workflows/tests.yml
git commit -m "Initial vimgym formula (placeholder sha256, populated on first release)"
git push -u origin main
```

The formula committed here has a `REPLACE_WITH_PYPI_SHA256_AFTER_FIRST_PUBLISH`
sentinel for `sha256` and **no resource blocks**. Both will be populated in
Step 1.6 below, after PyPI has the published artifact.

### Step 1.3 — Configure PyPI Trusted Publishing (the "pending publisher")

You already have the PyPI form open at <https://pypi.org/manage/account/publishing/>.

Fill it in with these exact values, then click **Add**:

| Field | Value |
| --- | --- |
| PyPI Project Name | `vimgym` |
| Owner | `shoaibrain` |
| Repository name | `vimgym` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

This authorizes the `release.yml` workflow in `shoaibrain/vimgym` running
inside the `pypi` GitHub environment to publish to the PyPI project named
`vimgym` — without any API token. The first successful run will claim the
project name.

> **Important:** the warning banner on the form is correct. Until the project
> is created, anyone could in theory race to claim the name. Get to Step 1.5
> within a reasonable window after clicking Add.

### Step 1.4 — Create the `pypi` GitHub Environment in the source repo

GitHub → `shoaibrain/vimgym` → Settings → Environments → **New environment**
→ name it exactly `pypi` → save.

You don't need protection rules for v0.1.1. (For v1.0.0+, consider adding
"Required reviewers: shoaibrain" so that publishing requires manual approval
in the workflow run.)

### Step 1.5 — Create the Homebrew tap PAT and add it as a secret

You need a fine-grained Personal Access Token that the release workflow uses
to open PRs against the tap repo.

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. Token name: `vimgym-tap-bumper`.
3. Resource owner: `shoaibrain`.
4. Repository access → **Only select repositories** → `shoaibrain/homebrew-vimgym`.
5. Repository permissions:
   - Contents: **Read and write**
   - Pull requests: **Read and write**
   - Metadata: Read-only (default)
6. Generate token, copy it.
7. In the **source** repo (`shoaibrain/vimgym`) → Settings → Secrets and
   variables → Actions → **New repository secret**:
   - Name: `HOMEBREW_TAP_TOKEN`
   - Value: paste the PAT

This token is scoped to the tap repo only. If it leaks, the blast radius is
limited to the tap.

### Step 1.6 — Cut the v0.1.1 release (the rest of Part 1 happens automatically)

Now you're ready. From the main repo:

```bash
cd ~/vimgym
git pull
make release-check                          # local sanity: lint + test + build
git status                                  # must be clean
```

If everything is clean and green:

```bash
# Tag the release
git tag -a v0.1.1 -m "v0.1.1 — first official release"
git push origin v0.1.1
```

Now create the GitHub Release. This is what triggers `release.yml`:

```bash
gh release create v0.1.1 \
  --title "vimgym 0.1.1 — first official release" \
  --notes-file CHANGELOG.md \
  --verify-tag
```

(Or use the web UI: <https://github.com/shoaibrain/vimgym/releases/new>,
select tag `v0.1.1`, paste the relevant section of `CHANGELOG.md` into the body,
click **Publish release**.)

Watch the workflow:

```bash
gh run watch --workflow=release.yml
```

The workflow will:

1. ✅ Verify the version matches the tag.
2. ✅ Build sdist + wheel.
3. ✅ Validate the wheel includes `vimgym/defaults/redaction-rules.json`.
4. ✅ Publish to PyPI via OIDC (this claims the project name).
5. ✅ Upload artifacts to the GitHub Release.
6. ⏳ Wait for PyPI to serve the new version.
7. ❌ **Try to bump the tap formula → expected to fail on the first release**
   because the tap formula has a `REPLACE_WITH_PYPI_SHA256_AFTER_FIRST_PUBLISH`
   placeholder sha256 that `dawidd6/action-homebrew-bump-formula` cannot
   reconcile. **This is fine.** The PyPI publish (steps 1–6) succeeded.

### Step 1.7 — Manually populate the tap formula (one time only)

Now that v0.1.1 is on PyPI, fix up the formula by hand:

```bash
# Compute the real sha256
VERSION=0.1.1
URL="https://files.pythonhosted.org/packages/source/v/vimgym/vimgym-${VERSION}.tar.gz"
SHA=$(curl -fsSL "$URL" | shasum -a 256 | cut -d' ' -f1)
echo "sha256: $SHA"

# Edit the formula in the tap repo
cd ~/vimgym/homebrew-vimgym
sed -i.bak "s|REPLACE_WITH_PYPI_SHA256_AFTER_FIRST_PUBLISH|${SHA}|" Formula/vimgym.rb
rm Formula/vimgym.rb.bak

# Generate every transitive Python resource block (this is the magic step)
brew tap shoaibrain/vimgym                           # tap your own tap if needed
brew update-python-resources Formula/vimgym.rb
# update-python-resources reads `vimgym`'s install_requires from PyPI and
# writes a `resource "fastapi" do ... end` block for every transitive dep.
# This takes 30–90 seconds.

# Sanity-check the formula
brew audit --strict --new-formula Formula/vimgym.rb
brew install --build-from-source --verbose Formula/vimgym.rb

# Smoke test: vg should be on PATH and pass doctor
which vg                                              # /opt/homebrew/bin/vg
vg --version                                          # vimgym 0.1.1
vg doctor

# If everything is happy, commit and push
git add Formula/vimgym.rb
git commit -m "vimgym 0.1.1 — first published release"
git push origin main
```

After this push, anyone can:

```bash
brew tap shoaibrain/vimgym
brew install vimgym
```

**You are now live.**

---

## Part 2 — Cutting a release (every time after v0.1.1)

This is the steady-state release process. It is much shorter than Part 1
because all the plumbing is in place.

### Step 2.1 — Bump the version

Two files must agree (the workflow refuses to publish if they don't):

```bash
# Replace 0.1.2 below with the new version
NEW=0.1.2

sed -i.bak "s/^version = .*/version = \"${NEW}\"/" pyproject.toml
sed -i.bak "s/^__version__ = .*/__version__ = \"${NEW}\"/" src/vimgym/__init__.py
rm pyproject.toml.bak src/vimgym/__init__.py.bak
```

### Step 2.2 — Update `CHANGELOG.md`

Add a new section at the top:

```markdown
## [0.1.2] — YYYY-MM-DD

### Fixed
- ...

### Added
- ...

[0.1.2]: https://github.com/shoaibrain/vimgym/releases/tag/v0.1.2
```

### Step 2.3 — Local sanity check

```bash
make release-check                          # lint + test + build
```

### Step 2.4 — Commit, tag, push

```bash
git add pyproject.toml src/vimgym/__init__.py CHANGELOG.md
git commit -m "Release v${NEW}"
git tag -a "v${NEW}" -m "v${NEW}"
git push origin main "v${NEW}"
```

### Step 2.5 — Publish the GitHub Release

```bash
gh release create "v${NEW}" \
  --title "vimgym ${NEW}" \
  --notes "$(awk "/## \[${NEW}\]/,/## \[/" CHANGELOG.md | sed '$d')" \
  --verify-tag
```

### Step 2.6 — Watch the workflow

```bash
gh run watch --workflow=release.yml
```

The workflow now does **everything**:

1. Verifies version sync.
2. Builds + validates the wheel.
3. Publishes to PyPI via OIDC.
4. Uploads artifacts to the GitHub Release.
5. Waits for PyPI.
6. **Opens a PR** against `shoaibrain/homebrew-vimgym` bumping `url` and `sha256`.

### Step 2.7 — Review and merge the tap PR

```bash
gh pr list --repo shoaibrain/homebrew-vimgym
gh pr view <N> --repo shoaibrain/homebrew-vimgym
gh pr merge <N> --repo shoaibrain/homebrew-vimgym --squash --delete-branch
```

The PR runs `brew test-bot` against the formula on macOS runners before
you merge. Trust but verify.

### Step 2.8 — Smoke test in a fresh terminal

```bash
brew update
brew upgrade vimgym
vg --version              # should print the new version
vg doctor                 # should be all green
```

Done.

---

## Recovering from a failed release

| Failure | What happened | How to recover |
| --- | --- | --- |
| **CI red on `main`** before tagging | Tests failed | Fix the bug, push to `main`, re-tag with `git tag -d v0.1.x && git tag -a v0.1.x ...` (only safe if the tag was never pushed) |
| **`pypi` job fails** | Build error, version mismatch, network | Read the workflow log. If the version is already on PyPI, you cannot republish — bump and tag a new version. |
| **`pypi` job succeeds, `bump-homebrew` fails** | The release workflow's tap-bump step couldn't open the PR | The PyPI publish succeeded — users can already `pip install`. Re-run just the `bump-homebrew` job from the GitHub Actions UI. If that still fails, fall back to Part 1 Step 1.7 (manual formula edit). |
| **Tap PR exists but `brew test-bot` is red** | Formula syntax error or transitive dep changed | Pull the bump branch locally, fix the formula, push to that branch, the PR re-runs |
| **You shipped a broken release** | Bug found post-release | Bump the patch version (`0.1.2 → 0.1.3`), fix forward, cut a new release. **Never delete a published PyPI version** — PyPI yanks instead: `twine yank vimgym==0.1.2 --reason "shipped with broken X"` |

---

## Things you must never do

1. **Never** edit `pyproject.toml` version without also editing `src/vimgym/__init__.py`.
   The release workflow refuses to publish on mismatch.
2. **Never** force-push a release tag. Tags must be immutable so users can
   trust them.
3. **Never** delete a published PyPI version. Use `twine yank` instead.
4. **Never** commit secrets to either repo. The `HOMEBREW_TAP_TOKEN` lives
   only in GitHub Actions secrets.
5. **Never** skip CI by tagging directly without pushing the commit first.
   The release workflow checks out the tag, not `main`.
