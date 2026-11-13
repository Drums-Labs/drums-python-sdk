## What changed

<!-- One behaviour per pull request. Say what changed and why, in plain words. -->

## How it was tested

<!-- The command you ran and what it printed. CI runs `python -m unittest discover -s tests -v` on 3.9 to 3.13. -->

```bash
python3 -m unittest discover -s tests -v
```

## Checklist

- [ ] Tests added or updated for the behaviour that changed, including the error path
- [ ] `CHANGELOG.md` updated under the next version heading
- [ ] Docstrings say what the method sends, returns and raises
- [ ] No third-party runtime dependency added
- [ ] No tokens, keys, private hostnames or account identifiers in code, tests, fixtures or docs
- [ ] Commits are signed off (`git commit -s`, see CONTRIBUTING.md)
