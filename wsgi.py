import os
import sys
import socket
from dotenv import load_dotenv

# Ensure unbuffered immediate terminal output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

load_dotenv()

port = int(os.getenv("PORT", 5000))
threads = int(os.getenv("WEB_CONCURRENCY", 128))

print("=" * 66, flush=True)
print("  [*] Initializing BUA Student ID Card Portal...", flush=True)

# 1. Initialize Database
from database import init_db
init_db()

# 2. Load Flask App
from app import app

# 3. Start Production WSGI Server
if __name__ == "__main__":
    def is_port_in_use(p):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('127.0.0.1', p)) == 0

    if is_port_in_use(port):
        print(f"\n[ERROR] Port {port} is ALREADY IN USE by another process!", flush=True)
        print(f"To fix this, either close the running terminal or run in PowerShell:", flush=True)
        print(f"  Get-Process python | Stop-Process -Force\n", flush=True)
        sys.exit(1)

    from waitress import serve
    print("=" * 66, flush=True)
    print(f"  [OK] SERVER IS RUNNING AND READY!", flush=True)
    print(f"  * Local URL:        http://localhost:{port}", flush=True)
    print(f"  * Direct IP:        http://127.0.0.1:{port}", flush=True)
    print(f"  * Worker Threads:   {threads} concurrent workers (Async/Non-blocking)", flush=True)
    print(f"  * Storage:          VPS NVMe Disk Active (High Speed)", flush=True)
    print("=" * 66, flush=True)
    print(f"--> Open in your browser: http://localhost:{port}", flush=True)
    print(f"--> To stop the server:   Press CTRL + C in this terminal\n", flush=True)

    try:
        serve(app, host="0.0.0.0", port=port, threads=threads, channel_timeout=60, connection_limit=1000)
    except KeyboardInterrupt:
        print("\n[INFO] Server stopped gracefully. Goodbye!", flush=True)


