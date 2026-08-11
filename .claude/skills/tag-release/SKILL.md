---
name: tag-release
description: Cut a new Museletter release - bump versions, write the changelog, commit, tag, push, and watch the release workflows. Use when the user wants to release, ship, publish, or tag a new version.
---

# Cut a Museletter release

Pushing a `v*` tag triggers two workflows: `.github/workflows/release.yml`
(verifies tag = version, runs tests, builds sdist + wheel, publishes to PyPI
via trusted publishing, creates a GitHub release whose notes are this tag's
`CHANGELOG.md` section, then calls `homebrew.yml` to regenerate the tap
formula) and `.github/workflows/docker.yml` (multi-arch image to
`ghcr.io/sanketsaurav/museletter`, tagged `X.Y.Z`, `X.Y`, and `latest`).

Everything after the push is automated, so the work here is the part that
needs judgment: preflight, the version, and the changelog. The changelog
section must be written **before** tagging - CI reads it to build the release
notes, and falls back to a generated commit list if it can't find a section
matching the tag.

This repo uses **bump-then-tag**: version bumps and the changelog are committed
to `master` first, then the tag points at that commit. The version lives in
**two places that must agree** - `pyproject.toml` (`version`) and
`src/museletter/__init__.py` (`__version__`). The release workflow hard-fails
if either disagrees with the tag.

## 1. Preflight

Stop and report (don't tag) if any of these fail:

```sh
git rev-parse --abbrev-ref HEAD     # must be master
git status --porcelain              # must be empty
git fetch origin && git rev-list --count master..origin/master   # must be 0
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/ty check
.venv/bin/pytest -q
```

## 2. Pick the version

Current version: `pyproject.toml` `version`. Review what's shipping:

```sh
last=$(git describe --tags --abbrev=0 2>/dev/null) || last=""
git log ${last:+$last..}HEAD --oneline
```

Pre-1.0 conventions: **patch** for fixes and internal-only changes, **minor**
for features, new API surface, or anything behavior-breaking (schema changes,
new required config, changed endpoints). Propose a version with one-line
reasoning; if the changes argue for either, ask the user.

## 3. Bump versions (two places)

Edit both, then verify they agree:

- `pyproject.toml` → `version = "X.Y.Z"`
- `src/museletter/__init__.py` → `__version__ = "X.Y.Z"`

```sh
grep -m1 '^version' pyproject.toml
grep '__version__' src/museletter/__init__.py
```

## 4. Write the changelog

Update `CHANGELOG.md` (create it with a `# Changelog` header if missing),
inserting a new section at the top:

```markdown
## vX.Y.Z - YYYY-MM-DD

### Features
- …

### Fixes
- …
```

Write user-facing prose from the actual changes (read the diffs when commit
subjects aren't enough) - not raw commit subjects. Omit empty sections; fold
notable internals into a short `### Internal` section only when worth telling
users. Call out anything self-hosters must act on (new env vars, schema
changes, changed endpoints) in an `### Upgrade notes` section. For the first
release (no prior tag), write release highlights rather than a history.

## 5. Commit, tag, confirm, push

```sh
git add -A
git commit -m "Release vX.Y.Z"
git tag -a "vX.Y.Z" -m "vX.Y.Z"
```

No AI/tool attribution anywhere - commit, tag, or changelog.

**Confirm with the user before pushing** - show the version and the changelog
section, and note that pushing publishes to PyPI (irrevocably) and GHCR. Then:

```sh
git push origin master "vX.Y.Z"
```

## 6. Watch the workflows and verify

```sh
gh run list --limit 3                          # release.yml + docker.yml for the tag
gh run watch <release-run-id> --exit-status    # includes the nested homebrew job
gh run watch <docker-run-id> --exit-status
gh release view "vX.Y.Z"                       # assets: .tar.gz + .whl; notes = the changelog section
```

If the release notes came out as a commit list, the changelog section header
didn't match `## vX.Y.Z ` (look for a warning in the release run). Fix
`CHANGELOG.md` on master and re-sync just the notes - don't re-tag:

```sh
awk -v v="## vX.Y.Z " 'index($0, v) == 1 {f=1; next} /^## v/ {f=0} f' CHANGELOG.md > /tmp/notes.md
gh release edit "vX.Y.Z" --notes-file /tmp/notes.md
```

Verify the artifacts are actually consumable, then report the release URL:

```sh
uvx --with "museletter==X.Y.Z" museletter --help   # PyPI propagated
docker manifest inspect ghcr.io/sanketsaurav/museletter:X.Y.Z | grep -c architecture  # ≥ 2
gh api repos/sanketsaurav/homebrew-tap/contents/Formula/museletter.rb --jq .content \
  | base64 -d | grep -m1 'museletter-X'            # tap formula on the new version
```

## Failure modes

- **First release / PyPI publish fails with an OIDC error**: the trusted
  publisher isn't registered. The user must add it on pypi.org (project
  `museletter`, owner `sanketsaurav`, repo `museletter`, workflow
  `release.yml`, environment `pypi`), then re-run the failed workflow run -
  don't re-tag.
- **Workflow fails before anything published**: fix on master, delete and
  re-push the tag (`git tag -d vX.Y.Z && git push --delete origin vX.Y.Z`,
  re-tag, re-push).
- **PyPI publish succeeded but something later failed**: a PyPI version can
  never be reused, even after deletion - never re-tag. Fix forward and cut a
  patch release.
- **Docker workflow failed but PyPI succeeded**: re-run just the docker
  workflow run; it's idempotent (retags the same commit).
- **The homebrew job failed**: re-run that job from the release run, or
  dispatch it on its own once PyPI has the version -
  `gh workflow run homebrew.yml -f version=X.Y.Z`. It only rewrites the tap
  formula, so it's safe to repeat. A missing `HOMEBREW_TAP_TOKEN` (fine-grained
  PAT, contents:write on `sanketsaurav/homebrew-tap`) is the usual cause.
