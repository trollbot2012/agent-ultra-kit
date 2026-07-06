# Releasing

How a new version of agent-ultra-kit gets built, tagged, released on GitHub,
and published to PyPI. Publishing uses **PyPI Trusted Publishing (OIDC)** — no
API token is ever stored in the repo.

## One-time PyPI setup (do this before the first publish)

You only do this once, and it is entirely on PyPI's / GitHub's side — nothing
to commit.

1. **Create the PyPI project via a "pending" trusted publisher** (so no token
   is ever needed). Sign in at <https://pypi.org>, go to **Your account →
   Publishing → Add a pending publisher**, and enter:
   - PyPI Project Name: `agent-ultra-kit`
   - Owner: `trollbot2012`
   - Repository name: `agent-ultra-kit`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **(Recommended) Gate publishing on your approval.** In the GitHub repo:
   **Settings → Environments → `pypi` → Required reviewers → add yourself**.
   Now even a tag push pauses and waits for you to click **Approve** before it
   publishes. The `pypi` and `testpypi` environments already exist in the repo.
3. **(Optional) TestPyPI dry run.** Repeat step 1 at
   <https://test.pypi.org> with environment name `testpypi` if you want to
   rehearse a publish (see "Dry runs" below).

That's it — no `PYPI_API_TOKEN` secret, no long-lived credentials.

## Cutting a release

### 1. Bump the version (two files, must match)

The release workflow refuses to publish if these disagree with the tag:

- `pyproject.toml` → `[project] version = "X.Y.Z"`
- `src/agent_ultra/__init__.py` → `__version__ = "X.Y.Z"`

Follow [SemVer](https://semver.org): patch for fixes, minor for
backward-compatible features, major for breaking changes. Pre-1.0, breaking
changes may land in a minor bump — call them out in the changelog.

### 2. Update the changelog

Move the `[Unreleased]` items under a new `## [X.Y.Z] — YYYY-MM-DD` heading in
[CHANGELOG.md](../CHANGELOG.md) and refresh the compare links at the bottom.

### 3. Pre-flight checks (all must pass locally)

```bash
pytest -q                     # full suite, offline
agent-ultra doctor            # 9-point health check
agent-ultra demo              # ends with "DEMO PASSED"
python -m build               # sdist + wheel build clean
python -m twine check dist/*  # both PASSED
```

Commit the version bump + changelog on `main` and let CI go green first.

### 4. Tag and push

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

The tag push triggers `release.yml`: it runs the tests, verifies the tag equals
the package version, builds, runs `twine check`, and — only if all of that
passes — publishes to PyPI (pausing for your approval if you added the required
reviewer). Publishing is **skipped entirely** if any earlier step fails.

### 5. Create the GitHub release

```bash
gh release create vX.Y.Z --title "vX.Y.Z" --notes-from-tag
# or write notes explicitly / paste the changelog section:
gh release create vX.Y.Z --title "vX.Y.Z — short name" --notes "..."
```

Keep the existing **v0.1.0** GitHub release as the public baseline; new
releases are added alongside it, not replacing it.

## Dry runs (no publish)

- **Build + check only:** Actions → **release** → **Run workflow** → target
  `none`. Builds, tests, and `twine check`s the artifacts without publishing.
- **TestPyPI:** same, target `testpypi` (needs the TestPyPI pending publisher
  from setup step 3). Then verify:
  `pip install -i https://test.pypi.org/simple/ agent-ultra-kit`.

## Rollback / yank

PyPI releases are **immutable** — you cannot re-upload the same version. If a
bad release ships:

- **Yank it** (preferred): PyPI project → **Manage → Releases → Yank**. A
  yanked version stays installable only by exact pin, so it stops reaching new
  users without breaking anyone who already pinned it. Do this from the PyPI
  web UI.
- **Publish a fixed version** (`X.Y.Z+1`) with the fix. This is the real
  remedy — yanking alone leaves users without a good version.
- **GitHub side:** delete or mark the bad GitHub release as a pre-release, and
  delete the bad tag if it never published:
  `git push origin :vX.Y.Z && git tag -d vX.Y.Z`. Never delete a tag that
  already published to PyPI — the version number is burned regardless.

## Checklist before you tag

- [ ] `pyproject.toml` and `__init__.py` versions match the intended tag
- [ ] CHANGELOG updated with the new version section
- [ ] `pytest -q` green locally and on CI (`main`)
- [ ] `agent-ultra doctor` and `agent-ultra demo` pass
- [ ] `python -m build && twine check dist/*` clean
- [ ] one-time PyPI trusted publisher configured (first release only)
- [ ] (recommended) `pypi` environment has you as a required reviewer
