# Releasing kelpie-sre

A release is a tag. Pushing `v<version>` runs [`.github/workflows/release.yml`](../.github/workflows/release.yml):

1. **verify**: the tag, `pyproject.toml` and `kelpie.__version__` agree
   (`scripts/release_version.py`), the tagged commit is on `main`, and lint, import
   contracts and the unit tests pass.
2. **build**: the sdist and wheel (`uv build`); the wheel is installed in a clean
   environment and `kelpie --version` and `kelpie status` run.
3. **testpypi**: both files go to [TestPyPI](https://test.pypi.org/project/kelpie-sre/)
   with trusted publishing.
4. **testpypi-install**: the wheel TestPyPI serves for `<version>` is downloaded, must be
   byte-for-byte the one built in step 2, is installed (its dependencies from PyPI only:
   anyone can upload to TestPyPI) and the CLI runs.
5. **pypi**: the same files go to [PyPI](https://pypi.org/project/kelpie-sre/), after the
   `pypi` environment's reviewer approves.
6. **image** and **github-release**, once PyPI has the version: the container image
   `ghcr.io/ch8er/kelpie:<version>` (linux/amd64 and linux/arm64, with a build provenance
   attestation; `latest` only for a final release) and a GitHub release with the files
   and generated notes (marked pre-release for `a`, `b` and `rc` versions).

No API token is stored anywhere: PyPI and TestPyPI trust the workflow through OpenID
Connect ("trusted publishing"), and GHCR takes the workflow's own `GITHUB_TOKEN`.

## One-time setup

Do these once, before the first tag. The names must match exactly: owner `ch8er`,
repository `kelpie`, workflow file `release.yml`, environments `testpypi` and `pypi`.

### PyPI and TestPyPI

TestPyPI is a separate site with its own account.

1. Create an account on [pypi.org](https://pypi.org/account/register/) and on
   [test.pypi.org](https://test.pypi.org/account/register/), and turn on two-factor
   authentication on both (PyPI requires it to publish).
2. Check that the name is free: <https://pypi.org/project/kelpie-sre/> and
   <https://test.pypi.org/project/kelpie-sre/> should both be "Not Found".
3. On **PyPI**, add a *pending* publisher (the project does not exist yet; the first
   upload creates it): **Your account → Publishing → Add a new pending publisher →
   GitHub**, with
   - PyPI Project Name: `kelpie-sre`
   - Owner: `ch8er`
   - Repository name: `kelpie`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
4. On **TestPyPI**, the same at <https://test.pypi.org/manage/account/publishing/>, with
   Environment name `testpypi`.

A pending publisher does not reserve the name: if someone else registers `kelpie-sre`
first, it is invalidated. Release soon after setting it up, and check the name again
right before the first tag. After the first upload it becomes the project's normal
trusted publisher (**project → Manage → Publishing**).

### GitHub

1. **Environments** (repository **Settings → Environments → New environment**):
   - `testpypi`: no reviewers needed. Under *Deployment branches and tags*, choose
     *Selected branches and tags* and add the tag rule `v*`.
   - `pypi`: the same tag rule `v*`, and under *Required reviewers* add yourself, so
     nothing reaches PyPI before you approve the run (after TestPyPI has passed). Turn on
     *Prevent self-review* only if someone else should approve.
2. **Tags**: protect release tags so only maintainers can create them
   (**Settings → Rules → Rulesets → New tag ruleset**, target `v*`, restrict creations,
   updates and deletions; under *Bypass list* add the **Repository admin** role with bypass
   mode *Always*, since a bypass list takes roles, teams and apps, not single users).
   Deleting a broken release tag (below) relies on that bypass.
3. **Actions**: nothing to change; the workflow asks for the permissions it needs per job
   (`id-token` for publishing, `packages` for GHCR, `contents` for the release).
4. **Container image**, after the first release: the package
   `ghcr.io/ch8er/kelpie` is private at first. Open it under **Your profile → Packages →
   kelpie → Package settings** and set *Change visibility* to **Public**. A public package
   cannot be made private again. The workflow pushes it with the repository's token, so it
   is linked to the repository; if a later push is refused, check the package's
   *Manage Actions access* (or its inherited repository access) for the `kelpie`
   repository.

## Cutting a release

Check the release blockers in [the plan](plan.md) first (week 3: multi-host and the
whole-codebase security audit block the release); do not push the first tag before they
are done.

1. In a PR: set the version in `pyproject.toml` (`[project] version`) and
   `src/kelpie/__init__.py` (`__version__`), run `uv lock` (the lock file records the
   project version), update [CHANGELOG.md](../CHANGELOG.md), and set the classifier in
   `pyproject.toml` if the development status changes (`3 - Alpha` for the first
   pre-release). Merge it.
2. Tag the merge commit on `main` and push the tag:

   ```sh
   git switch main && git pull
   git tag -a v0.1.0a1 -m "kelpie-sre 0.1.0a1"
   git push origin v0.1.0a1
   ```

3. Watch **Actions → Release**. When TestPyPI is done, open the run and approve the `pypi`
   deployment.
4. Check: `pipx run --spec kelpie-sre==0.1.0a1 kelpie --version` (or
   `uvx --from kelpie-sre==0.1.0a1 kelpie --version`),
   `docker run --rm ghcr.io/ch8er/kelpie:0.1.0a1 --version`, and the release page.

Versions are PEP 440 and written the same in the tag and both files: `0.1.0`, `0.1.0a1`,
`0.1.0b1`, `0.1.0rc1`, `0.1.0.post1`. A tag that disagrees stops the release in *verify*,
before anything is built.

### When something fails

- **verify or build**: fix it in a PR, delete the tag (`git push --delete origin v…` and
  `git tag -d v…`), and tag the new commit.
- **TestPyPI or PyPI rejects the upload**: a version can be uploaded only once, even if
  it is deleted later. Bump the version (e.g. `0.1.0a2`) and tag again. "Invalid
  publisher" means the trusted publisher's owner, repository, workflow or environment
  does not match exactly.
- **testpypi-install**: TestPyPI already has this version, so a fix needs a new version:
  bump it and tag again. (A plain re-run of the failed job is fine when TestPyPI was only
  slow to list the upload.)
- **Re-running**: use *Re-run failed jobs*, not *Re-run all jobs*: a complete re-run
  builds new files, which PyPI refuses for a version it already has (TestPyPI skips them,
  and the install check then fails on the byte comparison).
- **image or github-release**: PyPI already has the version; re-run just the failed jobs
  from the run page.

## Using a published version

```sh
pipx install kelpie-sre            # or: uv tool install kelpie-sre
pip install --pre kelpie-sre       # pre-releases need --pre (or ==<version>)
docker pull ghcr.io/ch8er/kelpie:<version>
KELPIE_IMAGE=ghcr.io/ch8er/kelpie:<version> docker compose up -d --no-build
```

`docker compose` builds the image from the checkout unless `KELPIE_IMAGE` names a
published one and `--no-build` is given.
