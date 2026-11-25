#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
STATE=${TESSEL_STATE_DIR:-$HOME/.tessel}
KEY=${TESSEL_SSH_KEY:?set TESSEL_SSH_KEY to the path of the EC2 key file}
field() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$STATE/cluster.json" "$1"; }
HEAD=$(field "d['nodes']['tessel-head']['public_ip']")
CIDR=$(field "d.get('vpc_cidr', '172.31.0.0/16')")
OPTS="-i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
cmd=${1:-push}

if [ "$cmd" = "push" ]; then
  python3 "$ROOT/deploy/make_config.py" > /tmp/tessel-config.json
  sed "s#__VPC_CIDR__#$CIDR#" "$ROOT/deploy/nginx.conf" > /tmp/tessel-nginx.conf
  tar -C "$ROOT" -czf /tmp/tessel-app.tgz tessel
  scp $OPTS /tmp/tessel-app.tgz /tmp/tessel-config.json "$ROOT/deploy/tessel-app.service" ec2-user@$HEAD:/tmp/
  scp $OPTS /tmp/tessel-nginx.conf ec2-user@$HEAD:/tmp/nginx.conf
  rm -f /tmp/tessel-config.json
  ssh $OPTS ec2-user@$HEAD 'set -e
    umask 077
    mkdir -p /opt/tessel/app
    tar -C /opt/tessel/app -xzf /tmp/tessel-app.tgz
    cp /tmp/tessel-config.json /opt/tessel/app/config.json
    rm -f /tmp/tessel-config.json
    chmod 600 /opt/tessel/app/config.json
    sudo cp /tmp/nginx.conf /etc/nginx/nginx.conf
    sudo cp /tmp/tessel-app.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo nginx -t
    sudo systemctl enable nginx tessel-app
    sudo systemctl restart nginx tessel-app
    sleep 2
    systemctl is-active nginx tessel-app'
  echo "open http://$(field "d['alb_dns']")"
elif [ "$cmd" = "ui" ]; then
  scp $OPTS "$ROOT/tessel/static/index.html" ec2-user@$HEAD:/opt/tessel/app/tessel/static/index.html
  echo "ui updated"
elif [ "$cmd" = "pull" ]; then
  ssh $OPTS ec2-user@$HEAD 'tar -C /opt/tessel/app -czf /tmp/tessel-pull.tgz tessel --exclude=__pycache__'
  scp $OPTS ec2-user@$HEAD:/tmp/tessel-pull.tgz /tmp/tessel-pull.tgz
  tar -C "$ROOT" -xzf /tmp/tessel-pull.tgz
  echo "pulled tessel/ from head"
elif [ "$cmd" = "ssh" ]; then
  shift
  ssh $OPTS ec2-user@$HEAD "$@"
fi
