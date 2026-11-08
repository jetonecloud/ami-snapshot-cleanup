import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def moto_ec2(aws_credentials, monkeypatch):
    """A moto-backed EC2 client, with operations moto doesn't implement
    (DescribeLockedSnapshots, rbin ListRules, and the deregistrationProtection
    attribute of DescribeImageAttribute) stubbed out so the rest of the flow
    can run end to end."""
    import botocore.client

    original_make_api_call = botocore.client.BaseClient._make_api_call

    def patched(self, operation_name, kwargs):
        if operation_name == "DescribeLockedSnapshots":
            return {"Snapshots": []}
        if operation_name == "ListRules":
            return {"Rules": []}
        if operation_name == "DescribeImageAttribute" and kwargs.get("Attribute") == "deregistrationProtection":
            return {"ImageId": kwargs.get("ImageId"), "DeregistrationProtection": {"Value": "disabled"}}
        return original_make_api_call(self, operation_name, kwargs)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", patched)

    with mock_aws():
        yield boto3.client("ec2", region_name="us-east-1")


def iso_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ami(image_id, name, age_days, size_gb=8, snapshot_id=None, tags=None, public=False):
    """A dict matching the shape of one item in DescribeImages()['Images']."""
    return {
        "ImageId": image_id,
        "Name": name,
        "CreationDate": iso_days_ago(age_days),
        "Public": public,
        "Tags": tags or [],
        "BlockDeviceMappings": (
            [{"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": snapshot_id, "VolumeSize": size_gb}}]
            if snapshot_id
            else []
        ),
    }


def snapshot(snapshot_id, age_days, size_gb=8, tags=None, description=""):
    """A dict matching the shape of one item in DescribeSnapshots()['Snapshots']."""
    return {
        "SnapshotId": snapshot_id,
        "VolumeSize": size_gb,
        "StartTime": iso_days_ago(age_days),
        "Tags": tags or [],
        "Description": description,
    }


def register_real_ami(ec2, name: str, age_days: int, size_gb: int = 8):
    """Create a real AMI in moto with a controlled CreationDate (moto reflects this)."""
    volume = ec2.create_volume(AvailabilityZone="us-east-1a", Size=size_gb)
    snap = ec2.create_snapshot(VolumeId=volume["VolumeId"])
    image = ec2.register_image(
        Name=name,
        Architecture="x86_64",
        RootDeviceName="/dev/xvda",
        BlockDeviceMappings=[
            {"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": snap["SnapshotId"], "VolumeSize": size_gb}}
        ],
    )
    image_id = image["ImageId"]

    from moto.ec2.models import ec2_backends

    backend = ec2_backends["123456789012"]["us-east-1"]
    backend.amis[image_id].creation_date = datetime.now(timezone.utc) - timedelta(days=age_days)

    return image_id, snap["SnapshotId"]
