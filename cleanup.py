#!/usr/bin/env python3
"""Find unused AMIs and EBS snapshots, show their cost, and delete them if asked.

Dry run by default. See README.md for the full safety rules.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

LOG_FILE = "ami-snapshot-cleanup.log"

# Approximate standard EBS snapshot price, $/GB-month. These are rough and
# change over time -- verify against https://aws.amazon.com/ebs/pricing/
# before relying on the dollar figures this script prints.
SNAPSHOT_GB_MONTH_PRICE = {
    "us-east-1": 0.05,
    "us-east-2": 0.05,
    "us-west-1": 0.055,
    "us-west-2": 0.05,
    "eu-west-1": 0.053,
    "eu-west-2": 0.055,
    "eu-central-1": 0.0575,
    "ap-southeast-1": 0.057,
    "ap-southeast-2": 0.057,
    "ap-northeast-1": 0.057,
    "sa-east-1": 0.076,
}
DEFAULT_GB_MONTH_PRICE = 0.05
# Archive tier is roughly a quarter of standard price, with a 90-day minimum
# storage duration -- deleting or restoring an archived snapshot before that
# window still bills for the remaining days.
ARCHIVE_TIER_DISCOUNT = 0.25
ARCHIVE_MIN_DAYS = 90


def price_per_gb_month(region: str) -> float:
    return SNAPSHOT_GB_MONTH_PRICE.get(region, DEFAULT_GB_MONTH_PRICE)


@dataclass
class Decision:
    resource_id: str
    resource_type: str  # "ami" or "snapshot"
    region: str
    name: str
    created: str
    age_days: int
    size_gb: float
    action: str  # "delete" or "keep"
    reason: str
    snapshot_ids: list[str] = field(default_factory=list)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_aws_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def name_group_key(name: str) -> str:
    """Strip a trailing date/version/timestamp suffix to group AMI families.

    Matches things like "-2026-11-01", "-20261101", "-v12", or a long run of
    digits at the end of the name (an epoch timestamp or build number).
    """
    stripped = re.sub(
        r"[-_]?(\d{4}-\d{2}-\d{2}(T[\d:-]+Z?)?|\d{8}|v?\d{5,}|\d+)$", "", name
    )
    return stripped or name


def parse_exclude_tags(raw: list[str] | None) -> list[tuple[str, str]]:
    if not raw:
        return [("Keep", "true")]
    pairs = []
    for item in raw:
        if "=" not in item:
            raise ValueError(f"--exclude-tag must be KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        pairs.append((key, value))
    return pairs


def matches_exclude_tag(tags: list[dict[str, str]] | None, excludes: list[tuple[str, str]]) -> str | None:
    for tag in tags or []:
        for key, value in excludes:
            if tag.get("Key") == key and tag.get("Value") == value:
                return f"{key}={value}"
    return None


def is_permission_error(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in ("AccessDenied", "UnauthorizedOperation", "AccessDeniedException")


def format_snapshot_time(value: Any) -> str:
    """boto3 returns StartTime as a datetime; dict-based test fixtures use plain strings."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class RegionData:
    """Everything fetched for one region, plus notes about failed checks."""

    def __init__(self) -> None:
        self.images: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.locked_snapshot_ids: set[str] = set()
        self.in_use_image_ids: set[str] = set()
        self.referenced_image_ids: set[str] = set()
        self.recycle_bin_ami_rule = False
        self.recycle_bin_snapshot_rule = False
        self.warnings: list[str] = []
        # Which "unknown" checks mean we must skip rather than delete.
        self.instances_unknown = False
        self.references_unknown = False
        self.locks_unknown = False


def paginate(client: Any, operation: str, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    paginator = client.get_paginator(operation)
    items: list[dict[str, Any]] = []
    for page in paginator.paginate(**kwargs):
        items.extend(page.get(result_key, []))
    return items


def manual_paginate(call: Any, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token = None
    while True:
        if token:
            kwargs["NextToken"] = token
        resp = call(**kwargs)
        items.extend(resp.get(result_key, []))
        token = resp.get("NextToken")
        if not token:
            break
    return items


def fetch_region_data(ec2: Any, asg_client: Any, rbin_client: Any, data: RegionData) -> None:
    data.images = paginate(ec2, "describe_images", "Images", Owners=["self"])
    data.snapshots = paginate(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"])

    try:
        data.locked_snapshot_ids = {
            s["SnapshotId"] for s in manual_paginate(ec2.describe_locked_snapshots, "Snapshots")
        }
    except ClientError as exc:
        if is_permission_error(exc):
            data.locks_unknown = True
            data.warnings.append(
                "cannot check snapshot locks (missing ec2:DescribeLockedSnapshots) -- skipping snapshot deletions"
            )
        else:
            raise

    try:
        instances = paginate(ec2, "describe_instances", "Reservations")
        for reservation in instances:
            for instance in reservation.get("Instances", []):
                if instance.get("State", {}).get("Name") != "terminated":
                    image_id = instance.get("ImageId")
                    if image_id:
                        data.in_use_image_ids.add(image_id)
    except ClientError as exc:
        if is_permission_error(exc):
            data.instances_unknown = True
            data.warnings.append(
                "cannot check running instances (missing ec2:DescribeInstances) -- skipping AMI deletions"
            )
        else:
            raise

    try:
        templates = paginate(ec2, "describe_launch_templates", "LaunchTemplates")
        for template in templates:
            versions = ec2.describe_launch_template_versions(
                LaunchTemplateId=template["LaunchTemplateId"],
                Versions=["$Default", "$Latest"],
            ).get("LaunchTemplateVersions", [])
            for version in versions:
                image_id = version.get("LaunchTemplateData", {}).get("ImageId")
                if image_id:
                    data.referenced_image_ids.add(image_id)

        launch_configs = paginate(asg_client, "describe_launch_configurations", "LaunchConfigurations")
        for lc in launch_configs:
            if lc.get("ImageId"):
                data.referenced_image_ids.add(lc["ImageId"])

        groups = paginate(asg_client, "describe_auto_scaling_groups", "AutoScalingGroups")
        lt_cache: dict[str, dict[str, Any]] = {}

        def resolve_lt_ami(lt_spec: dict[str, Any]) -> None:
            lt_id = lt_spec.get("LaunchTemplateId")
            lt_name = lt_spec.get("LaunchTemplateName")
            version = lt_spec.get("Version") or "$Default"
            cache_key = f"{lt_id or lt_name}:{version}"
            if cache_key in lt_cache:
                image_id = lt_cache[cache_key].get("LaunchTemplateData", {}).get("ImageId")
            else:
                kwargs: dict[str, Any] = {"Versions": [version]}
                if lt_id:
                    kwargs["LaunchTemplateId"] = lt_id
                else:
                    kwargs["LaunchTemplateName"] = lt_name
                versions = ec2.describe_launch_template_versions(**kwargs).get(
                    "LaunchTemplateVersions", []
                )
                if versions:
                    lt_cache[cache_key] = versions[0]
                    image_id = versions[0].get("LaunchTemplateData", {}).get("ImageId")
                else:
                    image_id = None
            if image_id:
                data.referenced_image_ids.add(image_id)

        for group in groups:
            if group.get("LaunchTemplate"):
                resolve_lt_ami(group["LaunchTemplate"])
            if group.get("LaunchConfigurationName") and group["LaunchConfigurationName"]:
                pass  # already covered via describe_launch_configurations above
            mip = group.get("MixedInstancesPolicy")
            if mip:
                overrides = mip.get("LaunchTemplate", {})
                spec = overrides.get("LaunchTemplateSpecification")
                if spec:
                    resolve_lt_ami(spec)
    except ClientError as exc:
        if is_permission_error(exc):
            data.references_unknown = True
            data.warnings.append(
                "cannot check launch templates/configs/ASGs (missing permission) -- skipping AMI deletions"
            )
        else:
            raise

    for resource_type, rule_attr in (("EC2_IMAGE", "recycle_bin_ami_rule"), ("EBS_SNAPSHOT", "recycle_bin_snapshot_rule")):
        try:
            rules = paginate(rbin_client, "list_rules", "Rules", ResourceType=resource_type)
            setattr(data, rule_attr, len(rules) > 0)
        except ClientError as exc:
            if is_permission_error(exc):
                data.warnings.append(
                    f"cannot check Recycle Bin rules for {resource_type} (missing rbin:ListRules)"
                )
            else:
                raise


def is_shared_or_public(image: dict[str, Any], ec2: Any) -> tuple[bool, str | None]:
    if image.get("Public"):
        return True, None
    try:
        attr = ec2.describe_image_attribute(ImageId=image["ImageId"], Attribute="launchPermission")
        if attr.get("LaunchPermissions"):
            return True, None
    except ClientError as exc:
        if is_permission_error(exc):
            return False, "cannot check launch permissions (missing ec2:DescribeImageAttribute)"
        raise
    return False, None


def is_deregistration_protected(image: dict[str, Any], ec2: Any) -> tuple[bool, str | None]:
    try:
        attr = ec2.describe_image_attribute(ImageId=image["ImageId"], Attribute="deregistrationProtection")
        value = attr.get("DeregistrationProtection", {}).get("Value")
        return value == "enabled", None
    except ClientError as exc:
        if is_permission_error(exc):
            return False, "cannot check deregistration protection (missing ec2:DescribeImageAttribute)"
        raise


def is_backup_or_dlm_managed(snapshot: dict[str, Any]) -> str | None:
    tags = {t["Key"]: t.get("Value", "") for t in snapshot.get("Tags", [])}
    for key in tags:
        if key.startswith("aws:backup:"):
            return "managed by AWS Backup"
        if key.startswith("aws:dlm:"):
            return "managed by Data Lifecycle Manager"
    description = snapshot.get("Description", "") or ""
    if "Created by Amazon Data Lifecycle Manager" in description or "Created by DLM" in description:
        return "managed by Data Lifecycle Manager"
    if "AWS Backup" in description:
        return "managed by AWS Backup"
    return None


def evaluate_amis(
    ec2: Any,
    region: str,
    data: RegionData,
    older_than_days: int,
    name_prefix: str | None,
    keep_latest: int,
    excludes: list[tuple[str, str]],
) -> list[Decision]:
    decisions: list[Decision] = []
    candidates = data.images
    if name_prefix:
        candidates = [img for img in candidates if img.get("Name", "").startswith(name_prefix)]

    groups: dict[str, list[dict[str, Any]]] = {}
    for img in candidates:
        key = name_prefix if name_prefix else name_group_key(img.get("Name", img["ImageId"]))
        groups.setdefault(key, []).append(img)

    keep_ids: set[str] = set()
    for group in groups.values():
        sorted_group = sorted(group, key=lambda i: i.get("CreationDate", ""), reverse=True)
        for img in sorted_group[:keep_latest]:
            keep_ids.add(img["ImageId"])

    now = now_utc()
    for img in candidates:
        image_id = img["ImageId"]
        name = img.get("Name", "")
        created_raw = img.get("CreationDate", "")
        created = parse_aws_time(created_raw) if created_raw else now
        age_days = (now - created).days
        snapshot_ids = [
            bdm["Ebs"]["SnapshotId"]
            for bdm in img.get("BlockDeviceMappings", [])
            if bdm.get("Ebs", {}).get("SnapshotId")
        ]
        size_gb = sum(
            bdm.get("Ebs", {}).get("VolumeSize", 0) for bdm in img.get("BlockDeviceMappings", [])
        )

        # Called immediately within this same iteration, never stored -- safe
        # despite closing over the loop variables below.
        def decide(action: str, reason: str) -> Decision:
            return Decision(image_id, "ami", region, name, created_raw, age_days, size_gb, action, reason, snapshot_ids)  # noqa: B023

        exclude_match = matches_exclude_tag(img.get("Tags"), excludes)
        if exclude_match:
            decisions.append(decide("keep", f"excluded by tag {exclude_match}"))
            continue

        if age_days <= older_than_days:
            decisions.append(decide("keep", f"only {age_days}d old (threshold {older_than_days}d)"))
            continue

        if image_id in keep_ids:
            decisions.append(decide("keep", f"within newest {keep_latest} for its name group"))
            continue

        shared, shared_err = is_shared_or_public(img, ec2)
        if shared_err:
            decisions.append(decide("keep", f"skipped: {shared_err}"))
            continue
        if shared:
            decisions.append(decide("keep", "shared with other accounts or public -- usage can't be seen from here"))
            continue

        protected, prot_err = is_deregistration_protected(img, ec2)
        if prot_err:
            decisions.append(decide("keep", f"skipped: {prot_err}"))
            continue
        if protected:
            decisions.append(decide("keep", "deregistration protection enabled"))
            continue

        if data.instances_unknown:
            decisions.append(decide("keep", "skipped: could not verify instance usage"))
            continue
        if image_id in data.in_use_image_ids:
            decisions.append(decide("keep", "in use by a running/stopped EC2 instance"))
            continue

        if data.references_unknown:
            decisions.append(decide("keep", "skipped: could not verify launch template/config/ASG references"))
            continue
        if image_id in data.referenced_image_ids:
            decisions.append(decide("keep", "referenced by a launch template, launch configuration, or Auto Scaling group"))
            continue

        decisions.append(decide("delete", f"older than {older_than_days}d, not kept-latest, unused, unprotected"))

    return decisions


def evaluate_orphan_snapshots(
    region: str,
    data: RegionData,
    older_than_days: int,
    excludes: list[tuple[str, str]],
    include_orphans: bool,
) -> list[Decision]:
    decisions: list[Decision] = []
    if not include_orphans:
        return decisions

    all_ami_snapshot_ids: set[str] = set()
    for img in data.images:
        for bdm in img.get("BlockDeviceMappings", []):
            sid = bdm.get("Ebs", {}).get("SnapshotId")
            if sid:
                all_ami_snapshot_ids.add(sid)

    now = now_utc()
    for snap in data.snapshots:
        snapshot_id = snap["SnapshotId"]
        if snapshot_id in all_ami_snapshot_ids:
            continue  # backs a registered AMI, handled alongside that AMI

        name = next((t["Value"] for t in snap.get("Tags", []) if t["Key"] == "Name"), "")
        created_raw = snap.get("StartTime")
        created_raw = format_snapshot_time(created_raw)
        created = parse_aws_time(created_raw) if created_raw else now
        age_days = (now - created).days
        size_gb = snap.get("VolumeSize", 0)

        def decide(action: str, reason: str) -> Decision:
            return Decision(snapshot_id, "snapshot", region, name, created_raw, age_days, size_gb, action, reason)  # noqa: B023

        exclude_match = matches_exclude_tag(snap.get("Tags"), excludes)
        if exclude_match:
            decisions.append(decide("keep", f"excluded by tag {exclude_match}"))
            continue

        managed_by = is_backup_or_dlm_managed(snap)
        if managed_by:
            decisions.append(decide("keep", managed_by))
            continue

        if data.locks_unknown:
            decisions.append(decide("keep", "skipped: could not verify snapshot lock status"))
            continue
        if snapshot_id in data.locked_snapshot_ids:
            decisions.append(decide("keep", "snapshot is locked"))
            continue

        if age_days <= older_than_days:
            decisions.append(decide("keep", f"only {age_days}d old (threshold {older_than_days}d)"))
            continue

        decisions.append(decide("delete", "orphaned (no registered AMI uses it) and past age threshold"))

    return decisions


def snapshot_decisions_for_deleted_ami(
    ami_decision: Decision,
    region: str,
    data: RegionData,
    excludes: list[tuple[str, str]],
    kept_ami_ids: set[str],
) -> list[Decision]:
    """Decide the fate of snapshots backing an AMI that is being deleted."""
    decisions = []
    snaps_by_id = {s["SnapshotId"]: s for s in data.snapshots}

    other_amis_using = {
        sid: [
            img["ImageId"]
            for img in data.images
            if img["ImageId"] != ami_decision.resource_id and img["ImageId"] in kept_ami_ids
            for bdm in img.get("BlockDeviceMappings", [])
            if bdm.get("Ebs", {}).get("SnapshotId") == sid
        ]
        for sid in ami_decision.snapshot_ids
    }

    for sid in ami_decision.snapshot_ids:
        snap = snaps_by_id.get(sid)
        if snap is None:
            continue
        name = next((t["Value"] for t in snap.get("Tags", []) if t["Key"] == "Name"), "")
        size_gb = snap.get("VolumeSize", 0)
        created_raw = snap.get("StartTime")
        created_raw = format_snapshot_time(created_raw)

        def decide(action: str, reason: str) -> Decision:
            return Decision(sid, "snapshot", region, name, created_raw, ami_decision.age_days, size_gb, action, reason)  # noqa: B023

        if other_amis_using.get(sid):
            decisions.append(decide("keep", f"still used by {other_amis_using[sid][0]}"))
            continue

        exclude_match = matches_exclude_tag(snap.get("Tags"), excludes)
        if exclude_match:
            decisions.append(decide("keep", f"excluded by tag {exclude_match}"))
            continue

        managed_by = is_backup_or_dlm_managed(snap)
        if managed_by:
            decisions.append(decide("keep", managed_by))
            continue

        if data.locks_unknown:
            decisions.append(decide("keep", "skipped: could not verify snapshot lock status"))
            continue
        if sid in data.locked_snapshot_ids:
            decisions.append(decide("keep", "snapshot is locked"))
            continue

        decisions.append(decide("delete", f"backs deregistered AMI {ami_decision.resource_id}"))

    return decisions


def cost_estimate(decisions: list[Decision], region: str) -> float:
    price = price_per_gb_month(region)
    total = 0.0
    for d in decisions:
        if d.resource_type == "snapshot" and d.action == "delete":
            total += d.size_gb * price
    return total


def print_table(decisions: list[Decision]) -> None:
    headers = ["ID", "Type", "Name", "Created", "Age(d)", "Size(GB)", "Decision", "Reason"]
    rows = [
        [d.resource_id, d.resource_type, d.name, d.created[:10], str(d.age_days), str(d.size_gb), d.action, d.reason]
        for d in decisions
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(len(headers))))


def write_output(decisions: list[Decision], path: str) -> None:
    rows = [
        {
            "id": d.resource_id,
            "type": d.resource_type,
            "region": d.region,
            "name": d.name,
            "created": d.created,
            "age_days": d.age_days,
            "size_gb": d.size_gb,
            "decision": d.action,
            "reason": d.reason,
        }
        for d in decisions
    ]
    if path.endswith(".json"):
        with open(path, "w") as f:
            json.dump(rows, f, indent=2)
    elif path.endswith(".csv"):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise ValueError("--output file must end in .csv or .json")


def log_deletion(entries: list[str]) -> None:
    timestamp = now_utc().isoformat()
    with open(LOG_FILE, "a") as f:
        f.writelines(f"{timestamp} {entry}\n" for entry in entries)


def perform_deletions(ec2: Any, region: str, decisions: list[Decision]) -> tuple[int, int, list[str]]:
    succeeded = 0
    failed = 0
    log_entries: list[str] = []

    ami_decisions = [d for d in decisions if d.resource_type == "ami" and d.action == "delete"]
    snapshot_decisions = {
        d.resource_id: d for d in decisions if d.resource_type == "snapshot" and d.action == "delete"
    }

    for ami in ami_decisions:
        own_snapshots_to_delete = [sid for sid in ami.snapshot_ids if sid in snapshot_decisions]
        try:
            resp = ec2.deregister_image(ImageId=ami.resource_id, DeleteAssociatedSnapshots=True)
            succeeded += 1
            log_entries.append(f"deregistered AMI {ami.resource_id} ({region})")
            results = {r["SnapshotId"]: r["ReturnCode"] for r in resp.get("DeleteSnapshotResults", [])}
            for sid in own_snapshots_to_delete:
                if results.get(sid) == "success":
                    succeeded += 1
                    log_entries.append(f"deleted snapshot {sid} ({region}) via DeleteAssociatedSnapshots")
                else:
                    try:
                        ec2.delete_snapshot(SnapshotId=sid)
                        succeeded += 1
                        log_entries.append(f"deleted snapshot {sid} ({region})")
                    except ClientError as exc:
                        failed += 1
                        log_entries.append(f"FAILED to delete snapshot {sid} ({region}): {exc}")
                snapshot_decisions.pop(sid, None)
        except ClientError as exc:
            failed += 1
            log_entries.append(f"FAILED to deregister AMI {ami.resource_id} ({region}): {exc}")
            for sid in own_snapshots_to_delete:
                failed += 1
                log_entries.append(
                    f"skipped snapshot {sid} ({region}): AMI {ami.resource_id} failed to deregister"
                )
                snapshot_decisions.pop(sid, None)

    # Whatever's left are orphan snapshots (--include-orphan-snapshots), not tied to any AMI above.
    for snap in snapshot_decisions.values():
        try:
            ec2.delete_snapshot(SnapshotId=snap.resource_id)
            succeeded += 1
            log_entries.append(f"deleted snapshot {snap.resource_id} ({region})")
        except ClientError as exc:
            failed += 1
            log_entries.append(f"FAILED to delete snapshot {snap.resource_id} ({region}): {exc}")

    log_deletion(log_entries)
    return succeeded, failed, log_entries


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cleanup.py",
        description="Find unused AMIs and EBS snapshots in your AWS account, show what they cost, "
        "and delete them only when you explicitly ask and confirm. Dry run by default.",
    )
    parser.add_argument("--region", action="append", required=True, help="AWS region to scan. Repeatable.")
    parser.add_argument("--profile", help="AWS named profile to use.")
    parser.add_argument("--older-than", type=int, default=90, help="Only consider resources older than this many days (default: 90).")
    parser.add_argument("--name-prefix", help="Only consider AMIs whose name starts with this prefix.")
    parser.add_argument("--keep-latest", type=int, default=3, help="Always keep this many newest AMIs per name group (default: 3).")
    parser.add_argument("--exclude-tag", action="append", help="KEY=VALUE tag that excludes a resource from cleanup. Repeatable. Default: Keep=true.")
    parser.add_argument("--include-orphan-snapshots", action="store_true", help="Also consider snapshots not linked to any registered AMI.")
    parser.add_argument("--delete", action="store_true", help="Actually delete after showing the plan and confirming. Without this flag, nothing is ever changed.")
    parser.add_argument("--output", help="Write the plan to this file as CSV or JSON, based on the extension.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        excludes = parse_exclude_tags(args.exclude_tag)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()

    try:
        account_id = session.client("sts", region_name=args.region[0]).get_caller_identity()["Account"]
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        print(f"error: could not get AWS credentials or identity: {exc}", file=sys.stderr)
        return 2

    all_decisions: list[Decision] = []
    region_data: dict[str, RegionData] = {}

    region_clients: dict[str, Any] = {}

    for region in args.region:
        ec2 = session.client("ec2", region_name=region)
        asg_client = session.client("autoscaling", region_name=region)
        rbin_client = session.client("rbin", region_name=region)
        region_clients[region] = ec2

        data = RegionData()
        try:
            fetch_region_data(ec2, asg_client, rbin_client, data)
        except (ClientError, BotoCoreError) as exc:
            print(f"error: could not read AWS resources in {region}: {exc}", file=sys.stderr)
            return 2
        region_data[region] = data

        for warning in data.warnings:
            print(f"warning ({region}): {warning}", file=sys.stderr)
        if not data.recycle_bin_ami_rule:
            print(f"warning ({region}): no Recycle Bin retention rule covers AMIs -- deletions can't be recovered", file=sys.stderr)
        if not data.recycle_bin_snapshot_rule:
            print(f"warning ({region}): no Recycle Bin retention rule covers EBS snapshots -- deletions can't be recovered", file=sys.stderr)

        ami_decisions = evaluate_amis(ec2, region, data, args.older_than, args.name_prefix, args.keep_latest, excludes)
        all_decisions.extend(ami_decisions)

        kept_ami_ids = {d.resource_id for d in ami_decisions if d.action == "keep"}
        for ami in [d for d in ami_decisions if d.action == "delete"]:
            all_decisions.extend(
                snapshot_decisions_for_deleted_ami(ami, region, data, excludes, kept_ami_ids)
            )

        all_decisions.extend(
            evaluate_orphan_snapshots(region, data, args.older_than, excludes, args.include_orphan_snapshots)
        )

    print_table(all_decisions)

    if args.output:
        write_output(all_decisions, args.output)
        print(f"\nPlan written to {args.output}")

    delete_count = sum(1 for d in all_decisions if d.action == "delete")
    keep_count = sum(1 for d in all_decisions if d.action == "keep")
    total_monthly_estimate = sum(cost_estimate(all_decisions, region) for region in args.region)

    print(f"\n{delete_count} to delete, {keep_count} to keep.")
    print(
        f"Estimated monthly saving (upper bound, snapshots are incremental so real savings are "
        f"usually lower): ${total_monthly_estimate:.2f}. Archive-tier snapshots have a "
        f"{ARCHIVE_MIN_DAYS}-day minimum storage charge not reflected here."
    )

    if not args.delete:
        print("\nDry run only. No changes made. Re-run with --delete to remove these resources.")
        return 0

    if delete_count == 0:
        print("\nNothing to delete.")
        return 0

    print(f"\nTo confirm deletion, type the AWS account ID ({account_id}):")
    confirmation = input("> ").strip()
    if confirmation != account_id:
        print("Account ID did not match. Aborting -- nothing was deleted.", file=sys.stderr)
        return 2

    total_succeeded = 0
    total_failed = 0
    for region in args.region:
        region_decisions = [d for d in all_decisions if d.region == region]
        succeeded, failed, _log = perform_deletions(region_clients[region], region, region_decisions)
        total_succeeded += succeeded
        total_failed += failed

    print(f"\nDeleted {total_succeeded} resource(s). {total_failed} failure(s). See {LOG_FILE} for details.")
    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
