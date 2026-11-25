TEMPLATE = r"""#!/bin/bash
exec > /var/log/tessel-bootstrap.log 2>&1
set -x
dnf install -y libgomp
mkdir -p /opt/tessel/bin /opt/tessel/rpc-cache /opt/tessel/status
aws s3 cp s3://@@BUCKET@@/tessel-rpc.tgz /tmp/tessel-rpc.tgz
tar -xzf /tmp/tessel-rpc.tgz -C /opt/tessel/bin
chown -R ec2-user:ec2-user /opt/tessel
cat > /opt/tessel/start-rpc.sh <<'EOS'
#!/bin/bash
BIN=$(find /opt/tessel/bin -type f \( -name ggml-rpc-server -o -name rpc-server \) | head -1)
IP=$(hostname -I | awk '{print $1}')
export LD_LIBRARY_PATH=/opt/tessel/bin
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
systemctl daemon-reload
systemctl enable --now tessel-rpc
touch /opt/tessel/status/rpc_up
"""


def worker_userdata(bucket):
    return TEMPLATE.replace("@@BUCKET@@", bucket)
