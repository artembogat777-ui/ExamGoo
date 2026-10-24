import os
import signal
import subprocess
import time
from pathlib import Path


url_file = Path(os.getenv("TUNNEL_URL_FILE", "/shared/webapp_url"))
print("Ожидаю публичную ссылку Cloudflare Tunnel...", flush=True)
stopping = False
process = None


def stop(*_):
    global stopping
    stopping = True
    if process and process.poll() is None:
        process.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

while not stopping:
    try:
        url = url_file.read_text(encoding="utf-8").strip()
        if url.startswith("https://"):
            break
    except OSError:
        pass
    time.sleep(1)

while not stopping:
    try:
        url = url_file.read_text(encoding="utf-8").strip()
    except OSError:
        url = ""
    if not url.startswith("https://"):
        time.sleep(1)
        continue

    env = {**os.environ, "WEBAPP_URL": url}
    print(f"Запускаю bot.py с WEBAPP_URL={url}", flush=True)
    process = subprocess.Popen(["python", "bot.py"], env=env)
    while process.poll() is None and not stopping:
        try:
            current_url = url_file.read_text(encoding="utf-8").strip()
        except OSError:
            current_url = ""
        if current_url and current_url != url:
            print("Cloudflare URL изменился; перезапускаю бота", flush=True)
            process.terminate()
            break
        time.sleep(1)
    if process.poll() is None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    process = None
    if not stopping:
        time.sleep(3)