import os


def load(path=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = path or os.environ.get("TESSEL_ENV_FILE") or os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), os.path.expanduser(value.strip().strip('"').strip("'")))
