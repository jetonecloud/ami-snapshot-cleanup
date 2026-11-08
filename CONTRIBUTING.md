# Contributing

Thanks for considering a contribution. This project intentionally stays small -- one Python file,
standard library plus `boto3`, no extra moving parts. Please keep that in mind before adding a
dependency, a config file, or a new mode.

## Before you start

If you're planning something bigger than a small fix (a new flag, a new check, a new output
format), open an issue first to talk it through. Small bug fixes and doc improvements can go
straight to a pull request.

## Development setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install boto3 pytest moto ruff mypy
```

## Running checks locally

```
ruff check cleanup.py tests/
mypy cleanup.py
pytest -q
```

All three run in CI on every pull request.

## Guidelines

- No real AWS account is used in tests. Tests use `moto` and/or `botocore.Stubber` to simulate
  AWS responses, including permission errors and pagination.
- Every skip/keep rule in the README should have a corresponding test. If you add a rule, add a
  test for both the rule firing and the rule being correctly skipped when a permission check
  fails.
- Dry-run behavior must stay enforced by a test that asserts zero write API calls are made
  without `--delete`.
- Keep `cleanup.py` a single file. If a change would meaningfully grow it, consider whether it
  belongs in this tool at all -- see "Possible future additions" in the README for things that
  were deliberately left out of v1.
- No wildcard IAM actions in `iam/policy.json`, ever.

## Commit messages

Plain, present-tense, one logical change per commit. No need for a strict format beyond that.
