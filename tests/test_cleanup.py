from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from cleanup import (
    Decision,
    RegionData,
    build_arg_parser,
    cost_estimate,
    evaluate_amis,
    evaluate_orphan_snapshots,
    is_backup_or_dlm_managed,
    main,
    matches_exclude_tag,
    name_group_key,
    paginate,
    parse_exclude_tags,
    perform_deletions,
    snapshot_decisions_for_deleted_ami,
)
from tests.conftest import ami, register_real_ami, snapshot


def access_denied(operation="DescribeImageAttribute"):
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, operation)


def no_op_ec2(public=False, shared=False, protected=False, permission_error=None):
    """A Mock ec2 client answering describe_image_attribute calls."""
    client = Mock()

    def describe_image_attribute(ImageId, Attribute):
        if permission_error and Attribute == permission_error:
            raise access_denied("DescribeImageAttribute")
        if Attribute == "launchPermission":
            return {"LaunchPermissions": [{"UserId": "999999999999"}] if shared else []}
        if Attribute == "deregistrationProtection":
            return {"DeregistrationProtection": {"Value": "enabled" if protected else "disabled"}}
        raise AssertionError(f"unexpected attribute {Attribute}")

    client.describe_image_attribute.side_effect = describe_image_attribute
    return client


def fresh_region_data(images=None, snapshots=None, **flags):
    data = RegionData()
    data.images = images or []
    data.snapshots = snapshots or []
    for key, value in flags.items():
        setattr(data, key, value)
    return data


# ---- pure helpers ----


def test_name_group_key_strips_date_suffix():
    assert name_group_key("hardened-al2023-2026-11-01") == "hardened-al2023"


def test_name_group_key_strips_trailing_digits():
    assert name_group_key("myapp-1699999999") == "myapp"


def test_name_group_key_no_suffix_is_its_own_group():
    assert name_group_key("my-unique-ami") == "my-unique-ami"


def test_parse_exclude_tags_default():
    assert parse_exclude_tags(None) == [("Keep", "true")]


def test_parse_exclude_tags_custom():
    assert parse_exclude_tags(["Team=infra", "Env=prod"]) == [("Team", "infra"), ("Env", "prod")]


def test_parse_exclude_tags_bad_format():
    with pytest.raises(ValueError):
        parse_exclude_tags(["no-equals-sign"])


def test_matches_exclude_tag_hit_and_miss():
    tags = [{"Key": "Keep", "Value": "true"}]
    assert matches_exclude_tag(tags, [("Keep", "true")]) == "Keep=true"
    assert matches_exclude_tag(tags, [("Other", "x")]) is None
    assert matches_exclude_tag(None, [("Keep", "true")]) is None


def test_is_backup_or_dlm_managed_by_tag():
    snap = snapshot("snap-1", 100, tags=[{"Key": "aws:backup:source-resource", "Value": "x"}])
    assert is_backup_or_dlm_managed(snap) == "managed by AWS Backup"

    snap2 = snapshot("snap-2", 100, tags=[{"Key": "aws:dlm:lifecycle-policy-id", "Value": "x"}])
    assert is_backup_or_dlm_managed(snap2) == "managed by Data Lifecycle Manager"


def test_is_backup_or_dlm_managed_unmanaged():
    snap = snapshot("snap-3", 100)
    assert is_backup_or_dlm_managed(snap) is None


def test_paginate_collects_all_pages():
    client = Mock()
    paginator = Mock()
    paginator.paginate.return_value = iter([{"Images": ["a"]}, {"Images": ["b", "c"]}])
    client.get_paginator.return_value = paginator
    assert paginate(client, "describe_images", "Images") == ["a", "b", "c"]


# ---- evaluate_amis: one rule per test ----


def test_delete_candidate_happy_path():
    images = [ami("ami-1", "hardened-al2023-2020-01-01", age_days=200, snapshot_id="snap-1")]
    data = fresh_region_data(images=images)
    ec2 = no_op_ec2()
    decisions = evaluate_amis(ec2, "us-east-1", data, older_than_days=90, name_prefix=None, keep_latest=0, excludes=[("Keep", "true")])
    assert decisions[0].action == "delete"


def test_keep_too_young():
    images = [ami("ami-1", "app-2026-11-01", age_days=10, snapshot_id="snap-1")]
    data = fresh_region_data(images=images)
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "old" in decisions[0].reason


def test_keep_latest_grouping():
    images = [
        ami("ami-old", "app-2020-01-01", age_days=400, snapshot_id="s1"),
        ami("ami-mid", "app-2020-06-01", age_days=300, snapshot_id="s2"),
        ami("ami-new", "app-2020-09-01", age_days=200, snapshot_id="s3"),
    ]
    data = fresh_region_data(images=images)
    decisions = {d.resource_id: d for d in evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, keep_latest=2, excludes=[("Keep", "true")])}
    assert decisions["ami-old"].action == "delete"
    assert decisions["ami-mid"].action == "keep"
    assert decisions["ami-new"].action == "keep"


def test_keep_excluded_by_tag():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1", tags=[{"Key": "Keep", "Value": "true"}])]
    data = fresh_region_data(images=images)
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, 3, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "excluded" in decisions[0].reason


def test_keep_in_use_by_instance():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images, in_use_image_ids={"ami-1"})
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "instance" in decisions[0].reason


def test_keep_referenced_by_launch_template():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images, referenced_image_ids={"ami-1"})
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "launch template" in decisions[0].reason


def test_keep_shared_ami():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images)
    decisions = evaluate_amis(no_op_ec2(shared=True), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "shared" in decisions[0].reason


def test_keep_public_ami():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1", public=True)]
    data = fresh_region_data(images=images)
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "shared" in decisions[0].reason


def test_keep_deregistration_protected():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images)
    decisions = evaluate_amis(no_op_ec2(protected=True), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "protection" in decisions[0].reason


def test_keep_on_instance_check_unknown():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images, instances_unknown=True)
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "skipped" in decisions[0].reason


def test_keep_on_references_unknown():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images, references_unknown=True)
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "skipped" in decisions[0].reason


def test_keep_on_missing_permission_for_shared_check():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images)
    ec2 = no_op_ec2(permission_error="launchPermission")
    decisions = evaluate_amis(ec2, "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "skipped" in decisions[0].reason


def test_keep_on_missing_permission_for_protection_check():
    images = [ami("ami-1", "app-2020-01-01", age_days=200, snapshot_id="s1")]
    data = fresh_region_data(images=images)
    ec2 = no_op_ec2(permission_error="deregistrationProtection")
    decisions = evaluate_amis(ec2, "us-east-1", data, 90, None, 0, [("Keep", "true")])
    assert decisions[0].action == "keep"
    assert "skipped" in decisions[0].reason


def test_name_prefix_filters_other_amis_out():
    images = [
        ami("ami-1", "hardened-al2023-2020-01-01", age_days=200, snapshot_id="s1"),
        ami("ami-2", "unrelated-app-2020-01-01", age_days=200, snapshot_id="s2"),
    ]
    data = fresh_region_data(images=images)
    decisions = evaluate_amis(no_op_ec2(), "us-east-1", data, 90, "hardened-al2023", 3, [("Keep", "true")])
    assert len(decisions) == 1
    assert decisions[0].resource_id == "ami-1"


# ---- orphan snapshots ----


def test_orphan_snapshots_disabled_by_default():
    data = fresh_region_data(snapshots=[snapshot("snap-1", 200)])
    assert evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=False) == []


def test_orphan_snapshot_deleted_when_enabled():
    data = fresh_region_data(snapshots=[snapshot("snap-1", 200)])
    decisions = evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=True)
    assert decisions[0].action == "delete"


def test_orphan_snapshot_backing_ami_is_not_orphan():
    images = [ami("ami-1", "app", age_days=5, snapshot_id="snap-1")]
    data = fresh_region_data(images=images, snapshots=[snapshot("snap-1", 200)])
    decisions = evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=True)
    assert decisions == []


def test_orphan_snapshot_too_young():
    data = fresh_region_data(snapshots=[snapshot("snap-1", 5)])
    decisions = evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=True)
    assert decisions[0].action == "keep"


def test_orphan_snapshot_locked():
    data = fresh_region_data(snapshots=[snapshot("snap-1", 200)], locked_snapshot_ids={"snap-1"})
    decisions = evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=True)
    assert decisions[0].action == "keep"
    assert "locked" in decisions[0].reason


def test_orphan_snapshot_locks_unknown_skips():
    data = fresh_region_data(snapshots=[snapshot("snap-1", 200)], locks_unknown=True)
    decisions = evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=True)
    assert decisions[0].action == "keep"
    assert "skipped" in decisions[0].reason


def test_orphan_snapshot_backup_managed():
    snap = snapshot("snap-1", 200, tags=[{"Key": "aws:backup:source-resource", "Value": "x"}])
    data = fresh_region_data(snapshots=[snap])
    decisions = evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=True)
    assert decisions[0].action == "keep"
    assert "Backup" in decisions[0].reason


def test_orphan_snapshot_excluded_by_tag():
    snap = snapshot("snap-1", 200, tags=[{"Key": "Keep", "Value": "true"}])
    data = fresh_region_data(snapshots=[snap])
    decisions = evaluate_orphan_snapshots("us-east-1", data, 90, [("Keep", "true")], include_orphans=True)
    assert decisions[0].action == "keep"
    assert "excluded" in decisions[0].reason


# ---- snapshot_decisions_for_deleted_ami ----


def test_snapshot_deleted_alongside_deregistered_ami():
    images = [ami("ami-1", "app", age_days=200, snapshot_id="snap-1")]
    data = fresh_region_data(images=images, snapshots=[snapshot("snap-1", 200)])
    ami_decision = Decision("ami-1", "ami", "us-east-1", "app", "2020-01-01", 200, 8, "delete", "old", ["snap-1"])
    decisions = snapshot_decisions_for_deleted_ami(ami_decision, "us-east-1", data, [("Keep", "true")], kept_ami_ids=set())
    assert decisions[0].action == "delete"


def test_snapshot_kept_if_still_used_by_another_ami():
    images = [
        ami("ami-1", "app-old", age_days=200, snapshot_id="snap-1"),
        ami("ami-2", "app-new", age_days=1, snapshot_id="snap-1"),
    ]
    data = fresh_region_data(images=images, snapshots=[snapshot("snap-1", 200)])
    ami_decision = Decision("ami-1", "ami", "us-east-1", "app-old", "2020-01-01", 200, 8, "delete", "old", ["snap-1"])
    decisions = snapshot_decisions_for_deleted_ami(ami_decision, "us-east-1", data, [("Keep", "true")], kept_ami_ids={"ami-2"})
    assert decisions[0].action == "keep"
    assert "still used" in decisions[0].reason


def test_snapshot_kept_if_locked():
    images = [ami("ami-1", "app", age_days=200, snapshot_id="snap-1")]
    data = fresh_region_data(images=images, snapshots=[snapshot("snap-1", 200)], locked_snapshot_ids={"snap-1"})
    ami_decision = Decision("ami-1", "ami", "us-east-1", "app", "2020-01-01", 200, 8, "delete", "old", ["snap-1"])
    decisions = snapshot_decisions_for_deleted_ami(ami_decision, "us-east-1", data, [("Keep", "true")], kept_ami_ids=set())
    assert decisions[0].action == "keep"


# ---- cost estimate ----


def test_cost_estimate_only_counts_deleted_snapshots():
    decisions = [
        Decision("snap-1", "snapshot", "us-east-1", "", "2020", 200, 100, "delete", "x"),
        Decision("snap-2", "snapshot", "us-east-1", "", "2020", 200, 100, "keep", "x"),
        Decision("ami-1", "ami", "us-east-1", "", "2020", 200, 100, "delete", "x"),
    ]
    assert cost_estimate(decisions, "us-east-1") == pytest.approx(100 * 0.05)


# ---- deletion flow ----


def test_perform_deletions_uses_delete_associated_snapshots_and_handles_failures():
    ec2 = Mock()
    ec2.deregister_image.side_effect = [
        {"Return": True, "DeleteSnapshotResults": [{"SnapshotId": "snap-1", "ReturnCode": "success"}]},
        ClientError({"Error": {"Code": "InvalidAMIID.Unavailable", "Message": "gone"}}, "DeregisterImage"),
    ]
    decisions = [
        Decision("ami-1", "ami", "us-east-1", "a", "2020", 200, 8, "delete", "old", ["snap-1"]),
        Decision("snap-1", "snapshot", "us-east-1", "", "2020", 200, 8, "delete", "x"),
        Decision("ami-2", "ami", "us-east-1", "b", "2020", 200, 8, "delete", "old", ["snap-2"]),
        Decision("snap-2", "snapshot", "us-east-1", "", "2020", 200, 8, "delete", "x"),
    ]
    succeeded, failed, _log = perform_deletions(ec2, "us-east-1", decisions)
    assert succeeded == 2  # ami-1 deregistered + snap-1 deleted via DeleteAssociatedSnapshots
    assert failed == 2  # ami-2 deregister failed, and snap-2 is skipped because of it
    # snap-2 belonged to the failed AMI, so delete_snapshot should not have been called for it
    ec2.delete_snapshot.assert_not_called()


def test_perform_deletions_falls_back_to_explicit_delete_snapshot():
    ec2 = Mock()
    ec2.deregister_image.return_value = {
        "Return": True,
        "DeleteSnapshotResults": [{"SnapshotId": "snap-1", "ReturnCode": "skipped"}],
    }
    decisions = [
        Decision("ami-1", "ami", "us-east-1", "a", "2020", 200, 8, "delete", "old", ["snap-1"]),
        Decision("snap-1", "snapshot", "us-east-1", "", "2020", 200, 8, "delete", "x"),
    ]
    succeeded, failed, _log = perform_deletions(ec2, "us-east-1", decisions)
    ec2.delete_snapshot.assert_called_once_with(SnapshotId="snap-1")
    assert succeeded == 2
    assert failed == 0


# ---- CLI / main() integration ----


def test_help_exits_cleanly():
    with pytest.raises(SystemExit) as exc:
        build_arg_parser().parse_args(["--help"])
    assert exc.value.code == 0


def test_bad_exclude_tag_exits_2():
    rc = main(["--region", "us-east-1", "--exclude-tag", "no-equals"])
    assert rc == 2


def test_dry_run_makes_no_write_calls(moto_ec2, monkeypatch, capsys):
    from tests.conftest import register_real_ami

    image_id, _snapshot_id = register_real_ami(moto_ec2, "old-app-2020-01-01", age_days=200)

    import boto3 as real_boto3

    orig_client = real_boto3.Session.client

    calls = []

    def spy_client(self, service_name, **kwargs):
        client = orig_client(self, service_name, **kwargs)
        if service_name == "ec2":
            orig_deregister = client.deregister_image
            orig_delete_snap = client.delete_snapshot

            def spy_deregister(*a, **kw):
                calls.append("deregister_image")
                return orig_deregister(*a, **kw)

            def spy_delete(*a, **kw):
                calls.append("delete_snapshot")
                return orig_delete_snap(*a, **kw)

            client.deregister_image = spy_deregister
            client.delete_snapshot = spy_delete
        return client

    monkeypatch.setattr(real_boto3.Session, "client", spy_client)

    rc = main(["--region", "us-east-1", "--older-than", "90", "--keep-latest", "0"])

    assert rc == 0
    assert calls == []
    # the AMI must still exist -- nothing was actually deleted
    still_there = moto_ec2.describe_images(ImageIds=[image_id])["Images"]
    assert len(still_there) == 1


def test_delete_requires_matching_account_id(moto_ec2, monkeypatch):
    register_real_ami(moto_ec2, "old-app-2020-01-01", age_days=200)
    monkeypatch.setattr("builtins.input", lambda _: "000000000000")
    rc = main(["--region", "us-east-1", "--older-than", "90", "--keep-latest", "0", "--delete"])
    assert rc == 2


def test_delete_with_correct_account_id_succeeds(moto_ec2, monkeypatch):
    image_id, _snap = register_real_ami(moto_ec2, "old-app-2020-01-01", age_days=200)
    import boto3

    account_id = boto3.client("sts", region_name="us-east-1").get_caller_identity()["Account"]
    monkeypatch.setattr("builtins.input", lambda _: account_id)
    rc = main(["--region", "us-east-1", "--older-than", "90", "--keep-latest", "0", "--delete"])
    assert rc == 0
    remaining = moto_ec2.describe_images(Owners=["self"])["Images"]
    assert all(img["ImageId"] != image_id for img in remaining)
