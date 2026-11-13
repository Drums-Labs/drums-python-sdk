# Contributing to drums-sdk

Thanks for helping. This package is the Python client and MCP server for Drums; it uses only the standard library
and supports Python 3.9 and later. By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md).

## Set up

```bash
git clone https://github.com/Drums-Labs/drums-python-sdk.git
cd drums-python-sdk
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Run the tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests run against a fake gateway on `127.0.0.1` and need no Drums account or network. CI runs them on Python
3.9 through 3.13; run them on the oldest and the newest Python you have. If `uv` is installed, the whole matrix is:

```bash
for v in 3.9 3.10 3.11 3.12 3.13; do uv run --python "$v" --no-project python -m unittest discover -s tests; done
```

CI also builds the package and checks it:

```bash
.venv/bin/pip install --upgrade build twine
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*
```

## Make a change

- Keep the package dependency-free. A change that needs a third-party library needs a discussion in an issue first.
- Every public method gets a docstring that says what it sends, what it returns and which `drums.errors` it raises.
- Add or update a test in `tests/` for the behaviour you change, including the error path.
- A change to `drums/mcp_tools.json` must keep the list identical to the one the gateway serves; say in the PR which
  gateway change it pairs with.
- Update `CHANGELOG.md` under the next version heading.
- Do not put tokens, keys, hostnames of private machines, account identifiers or prices in code, tests, fixtures or
  docs.

## Sign your commits (DCO)

Every commit needs a `Signed-off-by` line with your real name and email, which states that you wrote the change or
have the right to submit it under the project's license, as set out in the
[Developer Certificate of Origin](https://developercertificate.org/). Git adds it for you:

```bash
git commit -s -m "step: return the observation when screenshot=True"
```

To add it to the last commit:

```bash
git commit --amend -s --no-edit
```

## Open a pull request

- Small, focused pull requests: one behaviour per PR.
- Fill in the template: what changed, how you tested it. CI must be green.
- Commit messages describe the change in plain words, in the present tense, with the area first
  (`transport: ...`, `mcp: ...`, `tests: ...`). No generated boilerplate or tool attribution lines.
- A maintainer reviews within a few days. We may ask for changes or for the PR to be split.

## Releases

Maintainers cut releases by tagging `vX.Y.Z` on `main`; the release workflow builds the package, runs the tests
against the built wheel on every supported Python and publishes to PyPI with Trusted Publishing (see
[RELEASE.md](RELEASE.md)).

## Security

Found a security problem? Do not open a public issue. Follow [SECURITY.md](SECURITY.md).
