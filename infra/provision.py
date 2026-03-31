"""
infra/provision.py
==================
Provisions all AWS infrastructure for the Smart Billing & Invoice Management System:
  - IAM role + instance profile (SQS access via instance metadata — no credentials in code)
  - SQS queue  (async invoice processing)
  - EC2 security group
  - EC2 t3.micro instance (Amazon Linux 2023, eu-north-1)
    → user-data installs Python deps, clones repo, starts systemd service

Usage:
    python infra/provision.py

Prerequisites:
  - .env with AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
  - cloud-key-pair.pem key pair already created in AWS (eu-north-1)
  - GitHub repo is public or EC2 has access
"""

import os
import json
import time
import textwrap

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
AWS_REGION         = os.getenv("AWS_REGION", "eu-north-1")
AMI_ID             = "ami-0cc38fb663faa09c2"   # Amazon Linux 2023, eu-north-1
INSTANCE_TYPE      = "t3.micro"
KEY_NAME           = "cloud-key-pair"
GITHUB_REPO        = "https://github.com/Prithviiixx/api-project"
SG_NAME            = "invoice-api-sg"
QUEUE_NAME         = "invoice-processing-queue"
ROLE_NAME          = "invoice-api-ec2-role"
PROFILE_NAME       = "invoice-api-instance-profile"
INSTANCE_TAG_NAME  = "invoice-api-server"


# ─── Clients ──────────────────────────────────────────────────────────────────
def make_clients():
    """Return (ec2, sqs, iam) clients using credentials from environment."""
    session = boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=AWS_REGION,
    )
    return (
        session.client("ec2"),
        session.client("sqs"),
        session.client("iam"),   # IAM is a global service
    )


# ─── IAM Role ─────────────────────────────────────────────────────────────────
def ensure_iam_role(iam) -> str:
    """Create EC2 role with SQS access. Idempotent — reuses if already exists."""
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })

    try:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=trust_policy,
            Description="Role for Invoice API EC2 instance",
        )
        iam.attach_role_policy(
            RoleName=ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/AmazonSQSFullAccess",
        )
        print(f"[IAM] Role '{ROLE_NAME}' created and SQS policy attached.")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "EntityAlreadyExists":
            print(f"[IAM] Role '{ROLE_NAME}' already exists — reusing.")
        else:
            raise

    try:
        iam.create_instance_profile(InstanceProfileName=PROFILE_NAME)
        iam.add_role_to_instance_profile(
            InstanceProfileName=PROFILE_NAME,
            RoleName=ROLE_NAME,
        )
        print(f"[IAM] Instance profile '{PROFILE_NAME}' created.")
        print("[IAM] Waiting 15 s for IAM profile to propagate…")
        time.sleep(15)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "EntityAlreadyExists":
            print(f"[IAM] Instance profile '{PROFILE_NAME}' already exists — reusing.")
        else:
            raise

    return PROFILE_NAME


# ─── SQS Queue ────────────────────────────────────────────────────────────────
def ensure_sqs_queue(sqs) -> str:
    """Create (or reuse) the SQS queue for invoice processing."""
    resp = sqs.create_queue(
        QueueName=QUEUE_NAME,
        Attributes={
            "DelaySeconds":           "0",
            "MessageRetentionPeriod": "86400",   # 1 day
            "VisibilityTimeout":      "30",
        },
    )
    url = resp["QueueUrl"]
    print(f"[SQS] Queue ready: {url}")
    return url


# ─── Security Group ───────────────────────────────────────────────────────────
def ensure_security_group(ec2) -> str:
    """Create SG that opens SSH (22), HTTP (80), and API (8000). Idempotent."""
    try:
        sg = ec2.create_security_group(
            GroupName=SG_NAME,
            Description="Smart Invoice API - SSH + HTTP + API port",
        )
        sg_id = sg["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 22,   "ToPort": 22,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
                {"IpProtocol": "tcp", "FromPort": 80,   "ToPort": 80,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}]},
                {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "FastAPI"}]},
            ],
        )
        print(f"[EC2] Security group created: {sg_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "InvalidGroup.Duplicate":
            sg_id = ec2.describe_security_groups(
                GroupNames=[SG_NAME]
            )["SecurityGroups"][0]["GroupId"]
            print(f"[EC2] Security group '{SG_NAME}' already exists: {sg_id}")
        else:
            raise
    return sg_id


# ─── User Data ────────────────────────────────────────────────────────────────
def build_user_data(sqs_queue_url: str) -> str:
    """
    Cloud-init script run on first boot.
    Clones the repo, installs deps, writes .env (no AWS secrets — uses IAM role),
    and starts a systemd service.
    """
    return textwrap.dedent(f"""\
        #!/bin/bash
        set -e
        exec > /var/log/user-data.log 2>&1

        echo "=== Starting Smart Invoice API setup ==="

        # System packages
        yum update -y
        yum install -y python3 python3-pip git

        # Clone repo
        git clone {GITHUB_REPO} /home/ec2-user/app
        chown -R ec2-user:ec2-user /home/ec2-user/app

        # Python deps
        pip3 install -r /home/ec2-user/app/requirements.txt

        # Environment config (no AWS credentials — IAM role provides them)
        cat > /home/ec2-user/app/.env << 'ENVEOF'
        AWS_REGION={AWS_REGION}
        SQS_QUEUE_URL={sqs_queue_url}
        ENVEOF
        chown ec2-user:ec2-user /home/ec2-user/app/.env

        # Systemd service
        cat > /etc/systemd/system/invoice-api.service << 'SVCEOF'
        [Unit]
        Description=Smart Billing & Invoice Management API
        After=network.target

        [Service]
        User=ec2-user
        WorkingDirectory=/home/ec2-user/app
        ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
        Restart=always
        RestartSec=5
        EnvironmentFile=/home/ec2-user/app/.env

        [Install]
        WantedBy=multi-user.target
        SVCEOF

        systemctl daemon-reload
        systemctl enable invoice-api
        systemctl start invoice-api

        echo "=== Setup complete ==="
    """)


# ─── Launch EC2 ───────────────────────────────────────────────────────────────
def launch_instance(ec2, sg_id: str, profile_name: str, sqs_queue_url: str) -> tuple:
    """Launch the EC2 instance, wait for running state, return (instance_id, public_ip)."""
    resp = ec2.run_instances(
        ImageId=AMI_ID,
        InstanceType=INSTANCE_TYPE,
        KeyName=KEY_NAME,
        MinCount=1,
        MaxCount=1,
        SecurityGroupIds=[sg_id],
        UserData=build_user_data(sqs_queue_url),
        IamInstanceProfile={"Name": profile_name},
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": INSTANCE_TAG_NAME}],
        }],
    )

    instance_id = resp["Instances"][0]["InstanceId"]
    print(f"[EC2] Instance launched: {instance_id}")
    print("[EC2] Waiting for instance to reach 'running' state…")

    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    desc = ec2.describe_instances(InstanceIds=[instance_id])
    inst = desc["Reservations"][0]["Instances"][0]
    public_ip = inst.get("PublicIpAddress", "N/A")

    print(f"[EC2] Instance running. Public IP: {public_ip}")
    return instance_id, public_ip


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("══════════════════════════════════════════════════════")
    print("  Smart Billing & Invoice Management — AWS Provisioner")
    print("══════════════════════════════════════════════════════\n")

    ec2, sqs, iam = make_clients()

    profile_name  = ensure_iam_role(iam)
    queue_url     = ensure_sqs_queue(sqs)
    sg_id         = ensure_security_group(ec2)
    instance_id, public_ip = launch_instance(ec2, sg_id, profile_name, queue_url)

    print(f"""
╔══════════════════════════════════════════════════════════╗
  Provisioning Complete!
╠══════════════════════════════════════════════════════════╣
  Instance ID  : {instance_id}
  Public IP    : {public_ip}
  App URL      : http://{public_ip}:8000
  API Docs     : http://{public_ip}:8000/docs
  SQS Queue    : {queue_url}
  SSH          : ssh -i cloud-key-pair.pem ec2-user@{public_ip}

  ⚠  User-data script runs in background on first boot.
     Wait ~2 minutes before accessing the app.

╠══════════════════════════════════════════════════════════╣
  GitHub Actions Secrets to add:
    EC2_HOST      = {public_ip}
    EC2_USERNAME  = ec2-user
    EC2_SSH_KEY   = <paste cloud-key-pair.pem contents>
    SQS_QUEUE_URL = {queue_url}
╚══════════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    main()
