# Contributing to drums-sdk

Thanks for helping. This package is the Python client for Drums; it uses only the standard library and supports
Python 3.9 and later.

## Set up

```bash
git clone https://github.com/Drums-Labs/drums-python-sdk.git
cd drums-python-sdk
python3 -m venv .venv && .venv/bin/pip install -e .
```

## Run the tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests run against a fake daemon on 127.0.0.1 and need no Drums account. Run them on the oldest Python you have
as well as the newest; CI covers 3.9 through 3.13.

## Make a change

- Keep the package dependency-free. A change that needs a third-party library needs a discussion first.
- Every public method gets a docstring that says what it sends, what it returns and which `drums.errors` it raises.
- Add or update a test in `tests/` for the behaviour you change, including the error path.
- Update `CHANGELOG.md` under the next version heading.
- Do not put tokens, keys, hostnames of private machines or prices in code, tests, fixtures or docs.

## Open a pull request

- Small, focused PRs. One behaviour per PR.
- Describe what changed and how you tested it. CI must be green.
- Commit messages describe the change in plain words. No generated boilerplate or tool attribution lines.

## Releases

Maintainers cut releases by tagging `vX.Y.Z` on `main`; the release workflow builds the package, runs the tests on
every supported Python and publishes to PyPI (see `RELEASE.md`).

## Security

Found a security problem? Do not open a public issue. Email the maintainers at the address on
[drums.run](https://drums.run) and give us a few days to respond.
