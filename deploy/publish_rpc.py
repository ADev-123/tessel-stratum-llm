import json
import os
import secrets
import subprocess

import boto3

import envfile

envfile.load()

P = os.path.join(os.path.expanduser(os.environ.get("TESSEL_STATE_DIR", "~/.tessel")), "cluster.json")
KEY = os.environ["TESSEL_SSH_KEY"]
c = json.load(open(P))
ssm = boto3.client("ssm", region_name="us-east-1")
s3 = boto3.client("s3", region_name="us-east-1")
head = c["nodes"]["tessel-head"]

c["base_ami"] = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
if not c.get("artifact_bucket"):
    c["artifact_bucket"] = "tessel-artifacts-" + secrets.token_hex(5)
    s3.create_bucket(Bucket=c["artifact_bucket"])
    s3.put_public_access_block(Bucket=c["artifact_bucket"], PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
json.dump(c, open(P, "w"), indent=1)

remote = f"""set -e
cd /opt/tessel/llama.cpp
T=$(cmake --build build --target help | grep -oE '(ggml-)?rpc-server' | head -1)
sudo cmake --build build -j1 --target $T
rm -f /tmp/tessel-rpc.tgz
tar -czf /tmp/tessel-rpc.tgz -C build/bin .
python3 -c "import boto3; boto3.client('s3', region_name='us-east-1').upload_file('/tmp/tessel-rpc.tgz', '{c['artifact_bucket']}', 'tessel-rpc.tgz')"
"""
open("/tmp/remote_build.sh", "w").write(remote)
base = ["-i", KEY, "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=20"]
subprocess.run(["scp", "-q"] + base + ["/tmp/remote_build.sh", f"ec2-user@{head['public_ip']}:/tmp/remote_build.sh"], check=True)
subprocess.run(["ssh"] + base + [f"ec2-user@{head['public_ip']}", "bash /tmp/remote_build.sh"], check=True)
print("published s3://" + c["artifact_bucket"] + "/tessel-rpc.tgz")
