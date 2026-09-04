"""
export_data.py – Export database records to PostgreSQL SQL dump for VPS import.
Extracts all users, students, and audit logs from current DB (Neon / SQLite)
and writes them into deploy/bua_backup.sql ready for `psql bua_db < bua_backup.sql`.
"""
import os
import sys
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()
from database import get_db

def to_dict(row, cur):
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    if hasattr(cur, "description") and cur.description:
        cols = [col[0] if isinstance(col, tuple) else getattr(col, "name", str(col)) for col in cur.description]
        return dict(zip(cols, row))
    return {}

def clean_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    # String escaping for PostgreSQL
    s = str(v).replace("'", "''")
    return f"'{s}'"

def export_to_sql():
    os.makedirs("deploy", exist_ok=True)
    out_file = os.path.join("deploy", "bua_backup.sql")

    conn = get_db()
    cur = conn.cursor()

    print("[*] Exporting database records for VPS migration...")

    # Fetch users
    cur.execute("SELECT id, email, password_hash, full_name, role, college, student_id, is_active, email_verified, created_at FROM users ORDER BY id ASC")
    users = [to_dict(r, cur) for r in cur.fetchall()]

    # Fetch students
    cur.execute("SELECT id, student_id, full_name, year, college, email, image_path, registered_by, created_at, updated_at FROM students ORDER BY id ASC")
    students = [to_dict(r, cur) for r in cur.fetchall()]

    # Fetch audit logs
    cur.execute("SELECT id, user_id, action, target, detail, ip, created_at FROM audit_log ORDER BY id ASC")
    audit_logs = [to_dict(r, cur) for r in cur.fetchall()]

    with open(out_file, "w", encoding="utf-8") as f:
        f.write("-- ====================================================================\n")
        f.write("-- BUA Student ID Portal – Database Backup Dump for VPS Migration\n")
        f.write("-- ====================================================================\n\n")
        f.write("BEGIN;\n\n")

        # Users
        f.write(f"-- 1. USERS ({len(users)} records)\n")
        for u in users:
            cols = ["id", "email", "password_hash", "full_name", "role", "college", "student_id", "is_active", "email_verified"]
            vals = [clean_val(u.get(c)) for c in cols]
            f.write(f"INSERT INTO users ({', '.join(cols)}) VALUES ({', '.join(vals)}) ON CONFLICT (email) DO NOTHING;\n")
        f.write("SELECT setval('users_id_seq', COALESCE((SELECT MAX(id) FROM users), 1));\n\n")

        # Students
        f.write(f"-- 2. STUDENTS ({len(students)} records)\n")
        for s in students:
            cols = ["id", "student_id", "full_name", "year", "college", "email", "image_path", "registered_by"]
            vals = [clean_val(s.get(c)) for c in cols]
            f.write(f"INSERT INTO students ({', '.join(cols)}) VALUES ({', '.join(vals)}) ON CONFLICT (student_id) DO NOTHING;\n")
        f.write("SELECT setval('students_id_seq', COALESCE((SELECT MAX(id) FROM students), 1));\n\n")

        # Audit Logs
        f.write(f"-- 3. AUDIT LOGS ({len(audit_logs)} records)\n")
        for a in audit_logs:
            cols = ["id", "user_id", "action", "target", "detail", "ip"]
            vals = [clean_val(a.get(c)) for c in cols]
            f.write(f"INSERT INTO audit_log ({', '.join(cols)}) VALUES ({', '.join(vals)});\n")
        f.write("SELECT setval('audit_log_id_seq', COALESCE((SELECT MAX(id) FROM audit_log), 1));\n\n")

        f.write("COMMIT;\n")
        f.write("-- Backup export completed successfully.\n")

    print(f"[OK] Exported {len(users)} users, {len(students)} students, and {len(audit_logs)} audit records.")
    print(f"[OK] SQL dump saved to: {out_file}")

if __name__ == "__main__":
    export_to_sql()
