"""
sync_db.py – Sync data from Neon PostgreSQL (via HTTPS port 443) to local SQLite.
Useful when outbound PostgreSQL port 5432 is blocked by the local ISP/network.
"""
import os
import sys
import sqlite3
import requests
from dotenv import load_dotenv

load_dotenv()

# Force UTF-8 on Windows terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DB_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "instance", "students.db")


def sync_from_neon():
    if not DB_URL:
        print("[ERROR] DATABASE_URL is not set in .env")
        return False

    # Extract Neon host
    # e.g. postgresql://user:pass@ep-xyz-pooler.c-5.us-east-2.aws.neon.tech/dbname
    try:
        host_part = DB_URL.split("@")[1].split("/")[0]
        https_url = f"https://{host_part}/sql"
    except Exception as e:
        print(f"[ERROR] Could not parse host from DATABASE_URL: {e}")
        return False

    print(f"[1/4] Connecting to Neon via HTTPS (port 443): {https_url}...")
    headers = {
        "Neon-Connection-String": DB_URL,
        "Content-Type": "application/json"
    }

    try:
        # 1. Fetch Users
        res_u = requests.post(https_url, headers=headers, json={"query": "SELECT * FROM users;"}, timeout=15)
        if res_u.status_code != 200:
            print(f"[ERROR] Neon query failed ({res_u.status_code}): {res_u.text}")
            return False
        users = res_u.json().get("rows", [])
        print(f"[2/4] Fetched {len(users)} users from Neon.")

        # 2. Fetch Students
        res_s = requests.post(https_url, headers=headers, json={"query": "SELECT * FROM students;"}, timeout=15)
        students = res_s.json().get("rows", []) if res_s.status_code == 200 else []
        print(f"[3/4] Fetched {len(students)} students from Neon.")

        # 3. Write to local SQLite
        os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()

        for u in users:
            cur.execute("SELECT id FROM users WHERE email=?", (u["email"],))
            row = cur.fetchone()
            if row:
                cur.execute("""UPDATE users SET password_hash=?, full_name=?, role=?, college=?, student_id=?,
                               is_active=?, email_verified=?, verify_token=? WHERE id=?""",
                            (u["password_hash"], u["full_name"], u["role"], u.get("college"), u.get("student_id"),
                             1 if u["is_active"] else 0, 1 if u["email_verified"] else 0, u.get("verify_token"), row[0]))
            else:
                cur.execute("""INSERT INTO users (email, password_hash, full_name, role, college, student_id, is_active, email_verified, verify_token)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (u["email"], u["password_hash"], u["full_name"], u["role"], u.get("college"), u.get("student_id"),
                             1 if u["is_active"] else 0, 1 if u["email_verified"] else 0, u.get("verify_token")))

        for s in students:
            cur.execute("SELECT id FROM students WHERE student_id=?", (s["student_id"],))
            row = cur.fetchone()
            if row:
                cur.execute("""UPDATE students SET full_name=?, year=?, college=?, email=?, image_path=? WHERE id=?""",
                            (s["full_name"], s["year"], s["college"], s.get("email"), s["image_path"], row[0]))
            else:
                cur.execute("""INSERT INTO students (student_id, full_name, year, college, email, image_path)
                               VALUES (?,?,?,?,?,?)""",
                            (s["student_id"], s["full_name"], s["year"], s["college"], s.get("email"), s["image_path"]))

        conn.commit()
        conn.close()
        print(f"[4/4] Successfully synced {len(users)} users and {len(students)} students to local SQLite!")
        return True

    except Exception as e:
        print(f"[ERROR] Sync failed: {e}")
        return False


if __name__ == "__main__":
    sync_from_neon()
