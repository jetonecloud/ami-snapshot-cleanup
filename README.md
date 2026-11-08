# ami-snapshot-cleanup

A local script that finds AMIs and EBS snapshots in your AWS account that look unused, shows what
they cost, and deletes them only when you explicitly ask and confirm.

Dry run by default. No server, no scheduler, no Lambda, no Terraform, no config file. One Python
file you run from your own machine with your own AWS credentials.

Built by [Jet1](https://jetonecloud.com) to clean up after
[`hardened-al2023`](https://github.com/jetonecloud/hardened-al2023), which rebuilds its AMI weekly
and leaves old versions (and their snapshot storage cost) behind.

## Quick start

```
pip install boto3
python3 cleanup.py --region us-east-1
```

or, from a clone of this repo, without managing a virtualenv yourself:

```
pipx run --spec . ami-snapshot-cleanup --region us-east-1
```

That's a dry run. It never changes anything -- it only reads, decides, and prints a plan.

Sample output:

```
$ python3 cleanup.py --region us-east-1 --name-prefix hardened-al2023

ID                    Type      Name                              Created     Age(d)  Size(GB)  Decision  Reason
--------------------------------------------------------------------------------------------------------------------
ami-0a1b2c3d4e5f6g7h8  ami       hardened-al2023-2026-05-01         2026-05-01  191     8         delete    older than 90d, not kept-latest, unused, unprotected
snap-0a1b2c3d4e5f6g7h8 snapshot                                     2026-05-01  191     8         delete    backs deregistered AMI ami-0a1b2c3d4e5f6g7h8
ami-1b2c3d4e5f6g7h8i9  ami       hardened-al2023-2026-08-01         2026-08-01  99      8         keep      within newest 3 for its name group
ami-2c3d4e5f6g7h8i9j0  ami       hardened-al2023-2026-09-15         2026-09-15  54      8         keep      only 54d old (threshold 90d)
ami-3d4e5f6g7h8i9j0k1  ami       hardened-al2023-2026-10-01         2026-10-01  38      8         keep      only 38d old (threshold 90d)

2 to delete, 3 to keep.
Estimated monthly saving (upper bound, snapshots are incremental so real savings are usually
lower): $0.40. Archive-tier snapshots have a 90-day minimum storage charge not reflected here.

Dry run only. No changes made. Re-run with --delete to remove these resources.
```

Happy with the plan? Run it again with `--delete`. It shows the same plan, then asks you to type
your AWS account ID to confirm. There is no flag to skip that prompt.

```
python3 cleanup.py --region us-east-1 --name-prefix hardened-al2023 --delete
```

## Usage

```
python3 cleanup.py --region us-east-1                                   # dry run
python3 cleanup.py --region us-east-1 --older-than 180
python3 cleanup.py --region us-east-1 --name-prefix hardened-al2023 --keep-latest 4
python3 cleanup.py --region us-east-1 --region us-west-2                # multiple regions
python3 cleanup.py --region us-east-1 --output plan.csv
python3 cleanup.py --region us-east-1 --delete                         # delete after confirmation
```

| Flag | Default | Meaning |
|---|---|---|
| `--region` | (required) | AWS region to scan. Repeatable. |
| `--profile` | default profile | Named AWS profile to use. |
| `--older-than` | 90 | Only consider resources older than this many days. |
| `--name-prefix` | none | Only consider AMIs whose name starts with this. |
| `--keep-latest` | 3 | Always keep this many newest AMIs per name group. |
| `--exclude-tag` | `Keep=true` | `KEY=VALUE` tag that excludes a resource. Repeatable. |
| `--include-orphan-snapshots` | off | Also consider snapshots not linked to any registered AMI. |
| `--delete` | off | Actually delete, after showing the plan and confirming. |
| `--output` | none | Write the plan to a `.csv` or `.json` file. |

Credentials come from the standard AWS credential chain (environment, `~/.aws/credentials`,
SSO, instance role, etc.) via `--profile`. The script never accepts or stores access keys
directly.

## What counts as unused

An AMI is a delete candidate only if **all** of the following are true:

- It's older than `--older-than` days.
- It's not among the `--keep-latest` newest AMIs for its name group. (If you pass
  `--name-prefix`, the matching AMIs are one group. Otherwise, AMIs are grouped by name with a
  trailing date or version suffix stripped -- `hardened-al2023-2026-11-01` and
  `hardened-al2023-2026-10-01` are the same group; `hardened-al2023` alone, with no suffix, is its
  own group of one and is always kept.)
- It's not used by any EC2 instance in any state except terminated.
- It's not referenced by the default or latest version of any launch template, or by any launch
  configuration or Auto Scaling group.
- It's not shared with other accounts and not public. The script can't see who's using an AMI
  from another account, so it skips these and reports them rather than guessing.
- It does not have deregistration protection enabled. The script reports this and never disables
  it for you.
- It doesn't match an `--exclude-tag`.

A snapshot is only deleted:

- Alongside the AMI it backs, right after that AMI is deregistered, or
- With `--include-orphan-snapshots`, if it backs no AMI registered in this account and is older
  than `--older-than`.

A snapshot is always skipped, regardless of age, if it's:

- Managed by AWS Backup or Data Lifecycle Manager (detected by their tags) -- those policies own
  the resource's retention, not this script.
- Locked (EBS Snapshot Lock).
- Matching an `--exclude-tag`.
- Owned by another account.

**If a check can't be completed** -- usually a missing IAM permission -- the affected resource is
skipped and the reason is printed. An unknown result is never treated as safe to delete.

## Deleting

1. Without `--delete`, the script never changes anything. This is enforced by a test that asserts
   dry-run mode makes zero write API calls.
2. With `--delete`, it shows the plan, then asks you to type your AWS account ID to confirm.
   There's no flag to bypass this.
3. Each AMI is deregistered first (using EC2's `DeleteAssociatedSnapshots` option, so its own
   snapshots go with it in the same call where possible), then any snapshot that wasn't deleted
   that way is deleted explicitly.
4. If one deletion fails, the script continues and reports every failure at the end.
5. Every deletion is appended to a local log file, `ami-snapshot-cleanup.log`, with a timestamp.
6. Exit codes: `0` if everything succeeded (or there was nothing to do), `1` if some deletions
   failed, `2` for errors like bad arguments, missing credentials, or a confirmation that didn't
   match.

**Before your first real `--delete` run**, set up a
[Recycle Bin](https://docs.aws.amazon.com/ebs/latest/userguide/recycle-bin.html) retention rule
covering AMIs and EBS snapshots, so a mistaken deletion can be recovered within the retention
window. This script only checks whether such a rule exists and warns you if it doesn't -- it
never creates one for you.

## Permissions

See [`iam/policy.json`](iam/policy.json). It has two statements:

- A read-only statement, sufficient to run the script in dry-run mode and see the full plan.
- A write statement with exactly two actions needed for `--delete`: `ec2:DeregisterImage` and
  `ec2:DeleteSnapshot`.

No wildcard actions anywhere. If you only want to see what the script would do, attach just the
read-only statement.

## Cost estimates are an upper bound

EBS snapshots are incremental -- the first snapshot of a volume stores the full data, but later
snapshots only store the blocks that changed. This script has no way to know how much of a
snapshot's data is unique to it versus shared with other snapshots of the same volume, so it
estimates cost using the **full reported volume size**, which overstates the real figure.

The per-GB-month price used is a small built-in table of approximate regional prices (default
$0.05/GB-month if your region isn't listed) -- check
[the current EBS pricing page](https://aws.amazon.com/ebs/pricing/) for exact numbers. If any of
your snapshots are in the archive storage tier, note that archived snapshots have a 90-day
minimum storage duration: deleting or restoring one before that window still bills for the
remaining days, which this estimate does not account for.

Treat the printed number as "you are paying at most this much for these snapshots," not as a
promise of savings.

## Limitations

- **Shared AMI usage is invisible from here.** If an AMI is shared with another account, this
  script has no way to tell whether that account is using it, so it always skips and reports
  shared or public AMIs rather than guessing.
- **It only checks what AWS exposes:** running instances, launch templates, launch configurations,
  and Auto Scaling groups. It cannot see an AMI ID hardcoded in Terraform state, a CI pipeline, an
  SSM parameter, a Packer config, or a README. Review the plan before you delete anything.
- **Cost figures are estimates**, not bills. See the section above.
- This is a single-account, single-run tool. It does not schedule anything, does not talk to AWS
  Organizations, does not touch EBS volumes directly, and does not handle snapshots copied across
  regions.

## Possible future additions

Not in v1, kept out deliberately to keep this script simple. Worth considering later if there's
demand:

- Optionally creating the Recycle Bin rule instead of just checking for one.
- Detecting AMI IDs referenced in Terraform state files passed on the command line.
- A `--max-delete` safety cap per run.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE).

## Want help with your AWS costs?

If you'd rather not run scripts like this one by hand across a growing number of accounts,
[Jet1](https://jetonecloud.com) runs managed DevOps for teams who'd rather not. Get in touch if
you want a second pair of eyes on your AWS bill.
