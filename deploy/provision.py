import json, subprocess, time, os, urllib.request
import boto3
from botocore.exceptions import ClientError

import envfile

envfile.load()

R = "us-east-1"
ec2 = boto3.client("ec2", region_name=R)
elb = boto3.client("elbv2", region_name=R)
ssm = boto3.client("ssm", region_name=R)
MY_IP = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip() + "/32"
_vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
VPC = _vpc["VpcId"]
VPC_CIDR = _vpc["CidrBlock"]
SUBNETS = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [VPC]}, {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
AZ_SUBNET = next(s["SubnetId"] for s in SUBNETS if s["AvailabilityZoneId"] == "use1-az2")
ALB_SUBNETS = [AZ_SUBNET, next(s["SubnetId"] for s in SUBNETS if s["AvailabilityZoneId"] == "use1-az4")]
ITYPE = "r7g.medium"
OUT = os.path.join(os.path.expanduser(os.environ.get("TESSEL_STATE_DIR", "~/.tessel")), "cluster.json")
KEY_NAME = os.environ.get("TESSEL_KEY_NAME", "vockey")
PROFILE = os.environ.get("TESSEL_INSTANCE_PROFILE", "LabInstanceProfile")
TAG = [{"Key": "tessel:managed", "Value": "true"}]

sha = subprocess.run(["gh", "api", "repos/ggml-org/llama.cpp/commits/master", "--jq", ".sha"],
                     capture_output=True, text=True).stdout.strip()
print("llama.cpp commit:", sha)
ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]


def sg(name, desc):
    try:
        return ec2.create_security_group(GroupName=name, Description=desc, VpcId=VPC,
                                         TagSpecifications=[{"ResourceType": "security-group", "Tags": TAG}])["GroupId"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidGroup.Duplicate":
            return ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [VPC]}])["SecurityGroups"][0]["GroupId"]
        raise


alb_sg = sg("tessel-alb-sg", "public http to alb")
nodes_sg = sg("tessel-nodes-sg", "tessel nodes")


def allow(gid, perms):
    try:
        ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=perms)
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


allow(alb_sg, [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "public http"}]}])
allow(nodes_sg, [
    {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": nodes_sg}]},
    {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": MY_IP, "Description": "admin ssh"}]},
    {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "UserIdGroupPairs": [{"GroupId": alb_sg}]},
])

USERDATA = r"""#!/bin/bash
exec > /var/log/tessel-bootstrap.log 2>&1
set -x
ROLE=@@ROLE@@
SHA=@@SHA@@
mkdir -p /opt/tessel/models /opt/tessel/app /opt/tessel/status
dnf install -y gcc gcc-c++ cmake git make python3-pip nginx htop jq
chown -R ec2-user:ec2-user /opt/tessel
touch /opt/tessel/status/deps_done
if [ "$ROLE" = "head" ]; then
  nohup bash -c 'curl -fL -C - --retry 10 --retry-delay 5 -o /opt/tessel/models/Qwen3-30B-A3B-Q4_K_M.gguf https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf && touch /opt/tessel/status/model_done' > /var/log/tessel-download.log 2>&1 &
  pip3 install fastapi uvicorn httpx pyyaml psutil boto3
fi
cd /opt/tessel
git init llama.cpp && cd llama.cpp
git remote add origin https://github.com/ggml-org/llama.cpp.git
git fetch --depth 1 origin $SHA && git checkout FETCH_HEAD
SERVER=OFF
[ "$ROLE" = "head" ] && SERVER=ON
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_RPC=ON -DGGML_NATIVE=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=$SERVER
if [ "$ROLE" = "head" ]; then
  cmake --build build -j1 --target llama-server
else
  T=$(cmake --build build --target help | grep -oE '(ggml-)?rpc-server' | head -1)
  echo "rpc target: $T"
  cmake --build build -j1 --target $T
fi
touch /opt/tessel/status/build_done
if [ "$ROLE" != "head" ]; then
  cat > /opt/tessel/start-rpc.sh <<'EOS'
#!/bin/bash
BIN=$(find /opt/tessel/llama.cpp/build -type f \( -name ggml-rpc-server -o -name rpc-server \) | head -1)
IP=$(hostname -I | awk '{print $1}')
exec $BIN -H $IP -p 50052 -c
EOS
  chmod +x /opt/tessel/start-rpc.sh
  cat > /etc/systemd/system/tessel-rpc.service <<'EOS'
[Unit]
Description=Tessel RPC worker
After=network.target
[Service]
User=ec2-user
Environment=LLAMA_CACHE=/opt/tessel/rpc-cache
ExecStart=/opt/tessel/start-rpc.sh
Restart=always
[Install]
WantedBy=multi-user.target
EOS
  mkdir -p /opt/tessel/rpc-cache && chown ec2-user:ec2-user /opt/tessel/rpc-cache
  systemctl daemon-reload && systemctl enable --now tessel-rpc
  touch /opt/tessel/status/rpc_up
fi
"""


def launch(name, role, size):
    ud = USERDATA.replace("@@ROLE@@", role).replace("@@SHA@@", sha)
    r = ec2.run_instances(
        ImageId=ami, InstanceType=ITYPE, MinCount=1, MaxCount=1, KeyName=KEY_NAME, SubnetId=AZ_SUBNET,
        SecurityGroupIds=[nodes_sg], IamInstanceProfile={"Name": PROFILE}, UserData=ud,
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": size, "VolumeType": "gp2", "DeleteOnTermination": True}}],
        MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
        TagSpecifications=[{"ResourceType": "instance", "Tags": TAG + [{"Key": "Name", "Value": name}, {"Key": "tessel:role", "Value": role}]}])
    return r["Instances"][0]["InstanceId"]


nodes = {"tessel-head": launch("tessel-head", "head", 35),
         "tessel-worker-1": launch("tessel-worker-1", "worker", 30),
         "tessel-worker-2": launch("tessel-worker-2", "worker", 30),
         "tessel-worker-3": launch("tessel-worker-3", "worker", 30),
         "tessel-worker-4": launch("tessel-worker-4", "worker", 30)}
print("launched:", nodes)
ec2.get_waiter("instance_running").wait(InstanceIds=list(nodes.values()), WaiterConfig={"Delay": 5, "MaxAttempts": 40})
info = {}
for r in ec2.describe_instances(InstanceIds=list(nodes.values()))["Reservations"]:
    for i in r["Instances"]:
        name = next(t["Value"] for t in i["Tags"] if t["Key"] == "Name")
        info[name] = {"id": i["InstanceId"], "private_ip": i["PrivateIpAddress"], "public_ip": i.get("PublicIpAddress"),
                      "az": i["Placement"]["AvailabilityZone"]}
print(json.dumps(info, indent=1))

lb = elb.create_load_balancer(Name="tessel-alb", Subnets=ALB_SUBNETS, SecurityGroups=[alb_sg], Scheme="internet-facing",
                              Type="application", Tags=TAG)["LoadBalancers"][0]
elb.modify_load_balancer_attributes(LoadBalancerArn=lb["LoadBalancerArn"], Attributes=[
    {"Key": "idle_timeout.timeout_seconds", "Value": "300"},
    {"Key": "routing.http.drop_invalid_header_fields.enabled", "Value": "true"}])
tg = elb.create_target_group(Name="tessel-tg", Protocol="HTTP", Port=80, VpcId=VPC, TargetType="instance",
                             HealthCheckPath="/healthz", HealthCheckIntervalSeconds=15, HealthyThresholdCount=2,
                             UnhealthyThresholdCount=3, Tags=TAG)["TargetGroups"][0]
elb.register_targets(TargetGroupArn=tg["TargetGroupArn"], Targets=[{"Id": info["tessel-head"]["id"]}])
elb.create_listener(LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
                    DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}])
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump({"nodes": info, "alb_dns": lb["DNSName"], "alb_arn": lb["LoadBalancerArn"], "tg_arn": tg["TargetGroupArn"],
           "alb_sg": alb_sg, "nodes_sg": nodes_sg, "llama_sha": sha, "subnet": AZ_SUBNET, "vpc": VPC, "vpc_cidr": VPC_CIDR}, open(OUT, "w"), indent=1)
print("ALB:", lb["DNSName"])
