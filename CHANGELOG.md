# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-11-08

### Added

- Initial release: `cleanup.py` finds unused AMIs and EBS snapshots across one or more regions,
  shows an upper-bound cost estimate, and deletes them only with `--delete` and a typed account-ID
  confirmation.
- Skip/keep rules for in-use AMIs, launch template/config/ASG references, shared or public AMIs,
  deregistration-protected AMIs, exclude tags, AWS Backup/DLM-managed snapshots, locked snapshots,
  and `--keep-latest` per-name-group retention.
- `--include-orphan-snapshots` to also consider snapshots not backing any registered AMI.
- CSV/JSON plan export via `--output`.
- Recycle Bin rule check (warns, never creates a rule).
- `iam/policy.json` with separate read-only and delete-only statements.
- Test suite using `pytest` and `moto`, including a dry-run-makes-no-write-calls guarantee.
