#!/usr/bin/env python3
"""
تشغيل محلي: بيسحب البيانات كل 5 دقايق أثناء الجلسة ويفتح الداشبورد على
http://localhost:8000
"""
import http.server
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PORT = 8000
EVERY = 5 * 60


def in_session(now):
    # Sun(6)..Thu(3) in Python weekday terms: Mon=0 … Sun=6 ; EGX trades Sun–Thu
    return now.weekday() in (6, 0, 1, 2, 3) and (10, 0) <= (now.hour, now.minute) <= (14, 40)


def loop():
    first = True
    while True:
        now = datetime.now(ZoneInfo("Africa/Cairo"))
        if first or in_session(now):
            subprocess.run([sys.executable, str(ROOT / "collector.py")], cwd=ROOT)
            first = False
        time.sleep(EVERY)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    with socketserver.TCPServer(("", PORT), Quiet) as httpd:
        url = f"http://localhost:{PORT}"
        print(f"الداشبورد شغال على {url}  (Ctrl+C للإيقاف)")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        httpd.serve_forever()
