# Releasing `drums` to PyPI

The release is automated by `.github/workflows/release.yml`. A pushed tag `vX.Y.Z` builds, checks and tests the
package and publishes it to PyPI; a manual run of the workflow publishes to TestPyPI first. No tokens are stored in
the repository: publishing uses PyPI Trusted Publishing (OIDC). The commands below are everything the owner runs.

## 0. One-time decisions and setup (owner)

### 0.1 Confirm the license

`LICENSE` contains the Apache License 2.0 text (recommended, and what `pyproject.toml` declared from the first
commit). The SPDX line in `pyproject.toml` is commented out until you confirm; the workflow refuses to publish
while it is.

- To release under Apache-2.0: in `pyproject.toml` replace `# license = "Apache-2.0"` with `license = "Apache-2.0"`
  and add `"License :: OSI Approved :: Apache Software License",` to `classifiers`.
- To release under another license: replace the contents of `LICENSE`, set `license = "<SPDX id>"` and the matching
  classifier.

Commit that change on `main` before tagging.

### 0.2 Register the Trusted Publisher on PyPI

The `drums` project does not exist on PyPI yet, so register a **pending** publisher (it becomes the project's
publisher on the first upload). Signed in to pypi.org as the account that will own the project:

1. Open https://pypi.org/manage/account/publishing/
2. Under "Add a new pending publisher", choose **GitHub** and enter exactly:

   | Field | Value |
   |---|---|
   | PyPI project name | `drums` |
   | Owner | `Drums-Labs` |
   | Repository name | `drums-python-sdk` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

3. Do the same on TestPyPI at https://test.pypi.org/manage/account/publishing/ with the environment name
   `testpypi` (the other four fields identical). TestPyPI is a separate account; create one if needed.

### 0.3 Create the GitHub environments

In the repository: Settings -> Environments -> New environment. Create `pypi` and `testpypi`. For `pypi`, add
yourself as a required reviewer so a tag push cannot publish without a click from you. (Trusted Publishing is bound
to these environment names; the names must match the table above.)

### 0.4 Fallback: an API token instead of OIDC

Only if Trusted Publishing cannot be used: create a project-scoped API token on pypi.org and add it as the
repository secret `PYPI_API_TOKEN` (and `TEST_PYPI_API_TOKEN` for TestPyPI). The workflow uses a token when the secret
exists and OIDC otherwise. Never paste a token into a file, a command line or a chat.

## 1. Merge the release PR

Merge `pypi-release` into `main` (after step 0.1 is committed on it or on `main`). Confirm `main` is what you want
to ship:

```bash
git checkout main && git pull --ff-only
python3 -m unittest discover -s tests
python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])'
```

The printed version is the release version; `drums/__init__.py` must say the same (`__version__`).

## 2. TestPyPI first

Run the workflow by hand; it publishes to TestPyPI only:

```bash
gh workflow run release.yml --ref main
gh run watch
```

Then verify the TestPyPI package in a clean venv. `--extra-index-url` is not needed (the package has no
dependencies), and `--no-deps` makes sure nothing is pulled from anywhere else:

```bash
cd "$(mktemp -d)"
python3 -m venv v && v/bin/pip install --no-deps -i https://test.pypi.org/simple/ drums
v/bin/python -c 'import drums; print(drums.__version__); drums.Client'
```

Check https://test.pypi.org/project/drums/ : the README renders, the links are right, the license shows.

## 3. Tag and publish to PyPI

```bash
V=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
git tag -a "v$V" -m "drums $V"
git push origin "v$V"
gh run watch
```

The `publish to PyPI` job waits for your approval on the `pypi` environment (step 0.3); approve it in the Actions
tab. The job prints the SHA256 of every file it uploads.

## 4. Verify the public package

```bash
cd "$(mktemp -d)"
python3 -m venv v && v/bin/pip install drums
v/bin/python -c 'import drums; print(drums.__version__); drums.Client'
```

Then check https://pypi.org/project/drums/ and create the GitHub release from the tag:

```bash
gh release create "v$V" --title "drums $V" --notes-file <(awk "/^## $V/{f=1;next} /^## /{f=0} f" CHANGELOG.md)
```

## 5. After the first release

- Later releases: bump `version` in `pyproject.toml` and `__version__` in `drums/__init__.py` together, add a
  `CHANGELOG.md` entry, merge, then steps 2 to 4. The workflow fails if the tag and the version disagree.
- A version can be uploaded to PyPI exactly once. A broken release gets a new patch version, never a re-upload.
- Update the install instructions in the docs (`console.drums.run/docs/quickstart` and
  `/docs/reference/python-sdk` still describe installing from a wheel file) to `pip install drums`.
