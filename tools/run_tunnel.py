import os
import re
import signal
import subprocess
import time
from pathlib import Path


URL_FILE = Path(os.getenv("TUNNEL_URL_FILE", "/shared/webapp_url"))
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
process = None


def stop(*_):
    if process and process.poll() is None:
        process.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
URL_FILE.parent.mkdir(parents=True, exist_ok=True)
URL_FILE.unlink(missing_ok=True)

process = subprocess.Popen(
    ["cloudflared", "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", "http://web:8000"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

try:
    for line in process.stdout:
        print(line, end="", flush=True)
        match = URL_PATTERN.search(line)
        if match:
            URL_FILE.write_text(match.group(0), encoding="utf-8")
    raise SystemExit(process.wait())
finally:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()