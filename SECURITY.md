# Security

## Reporting a vulnerability

If you find a security issue in this project, please email **security@jetonecloud.com** instead
of opening a public issue. Include what you found, how to reproduce it, and its impact if you
can. We'll acknowledge within a few business days.

## What this script does with your credentials

`cleanup.py` uses the standard AWS credential chain (environment variables, shared config/
credentials files, SSO, an instance or container role) via `--profile`. It never asks for, accepts,
or stores an AWS access key or secret directly, and it never writes your credentials anywhere.

It makes AWS API calls directly from your machine to the regions you pass with `--region`. There
is no telemetry, no phone-home, and no third-party service involved.

## What this script can do to your AWS account

Run without `--delete`, it only calls read-only EC2/Auto Scaling/Recycle Bin describe/list APIs --
see the read-only statement in [`iam/policy.json`](iam/policy.json).

Run with `--delete`, after you confirm by typing your AWS account ID, it can call
`ec2:DeregisterImage` and `ec2:DeleteSnapshot` on resources it decided are safe to remove. It
never calls any other write or delete API, and it never touches resources owned by another
account or shared/public AMIs.

## Scope

This is a local script you run yourself. There's no server-side component, so there's no attack
surface beyond "a bug in this code does something you didn't intend in your AWS account." Treat
pull requests that touch the deletion logic, permission checks, or confirmation flow with extra
scrutiny.
