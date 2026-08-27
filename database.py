"""
database.py  –  Dual-backend DB layer
  • SQLite  when DATABASE_URL is empty  (local dev)
  • PostgreSQL  when DATABASE_URL is set  (production)
"""
import os, sqlite3
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

try:
    import psycopg2
    import psycopg2.extras
    HAS_PG = True
except ImportError:
    HAS_PG = False

import bcrypt

# ── helpers ────────────────────────────────────────────────────────────────
DB_URL      = os.getenv("DATABASE_URL", "")
USE_PG      = bool(DB_URL) and HAS_PG
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "instance", "students.db")

COLLEGES = [
    "كلية طب الأسنان",
    "كلية الصيدلة فارما D",
    "كلية صيدلة اكلينيكية",
     "كلية العلاج الطبيعي",
    "كلية الطب البيطري",
    "كلية تكنلوجيا علوم حيوية",
    "كلية العلوم الصحية التطبيقية",
    "كلية التمريض",
    "كلية  ذكاء اصطناعي وعلوم البيانات",
    "كلية بزنس وإدارة الأعمال",
    "كلية لغات وترجمة",
    "كلية الحقوق",
    "كلية الفنون الجميلة",
]

ROLES = {
    "superadmin": "مدير النظام",
    "admin":      "مسؤول كلية",
    "staff":      "موظف تسجيل",
    "student":    "طالب",
}


# ── connection factory ─────────────────────────────────────────────────────
def get_db():
    if USE_PG:
        conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def placeholder(n: int) -> str:
    """Return %s for PG or ? for SQLite, repeated n times."""
    ch = "%s" if USE_PG else "?"
    return ", ".join([ch] * n)


def ph() -> str:
    """Single placeholder."""
    return "%s" if USE_PG else "?"


# ── schema ─────────────────────────────────────────────────────────────────
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    full_name     TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'staff',
    college       TEXT,
    student_id    TEXT    UNIQUE,
    is_active     INTEGER NOT NULL DEFAULT 1,
    email_verified INTEGER NOT NULL DEFAULT 0,
    verify_token  TEXT,
    reset_token   TEXT,
    reset_expires TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS students (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    TEXT    NOT NULL UNIQUE,
    full_name     TEXT    NOT NULL,
    year          TEXT    NOT NULL,
    college       TEXT    NOT NULL,
    email         TEXT,
    image_path    TEXT    NOT NULL,
    registered_by INTEGER REFERENCES users(id),
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id),
    action     TEXT    NOT NULL,
    target     TEXT,
    detail     TEXT,
    ip         TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_student_id     ON students(student_id);
CREATE INDEX IF NOT EXISTS idx_year           ON students(year);
CREATE INDEX IF NOT EXISTS idx_college        ON students(college);
CREATE INDEX IF NOT EXISTS idx_year_college   ON students(year, college);
CREATE INDEX IF NOT EXISTS idx_fullname       ON students(full_name);
CREATE INDEX IF NOT EXISTS idx_user_student   ON users(student_id);
CREATE INDEX IF NOT EXISTS idx_audit_user     ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts       ON audit_log(created_at);
"""

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    full_name     TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'staff',
    college       TEXT,
    student_id    TEXT    UNIQUE,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    verify_token  TEXT,
    reset_token   TEXT,
    reset_expires TIMESTAMP,
    created_at    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS students (
    id            SERIAL PRIMARY KEY,
    student_id    TEXT    NOT NULL UNIQUE,
    full_name     TEXT    NOT NULL,
    year          TEXT    NOT NULL,
    college       TEXT    NOT NULL,
    email         TEXT,
    image_path    TEXT    NOT NULL,
    registered_by INTEGER REFERENCES users(id),
    created_at    TIMESTAMP DEFAULT NOW(),
    updated_at    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),
    action     TEXT    NOT NULL,
    target     TEXT,
    detail     TEXT,
    ip         TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_student_id ON students(student_id);
CREATE INDEX IF NOT EXISTS idx_year       ON students(year);
CREATE INDEX IF NOT EXISTS idx_college    ON students(college);
CREATE INDEX IF NOT EXISTS idx_user_student ON users(student_id);
CREATE INDEX IF NOT EXISTS idx_audit_user   ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(created_at);
"""


def init_db():
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    conn = get_db()
    cur  = conn.cursor()

    # ── Migration FIRST: add missing columns to existing tables ──
    if USE_PG:
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS student_id TEXT UNIQUE")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_student ON users(student_id)")
        except Exception:
            pass
        conn.commit()
    else:
        migrations = [
            "ALTER TABLE users ADD COLUMN student_id TEXT",
        ]
        for m in migrations:
            try:
                cur.execute(m)
            except Exception:
                pass  # column already exists
        conn.commit()

    # ── Create tables + indexes ──
    schema = _PG_SCHEMA if USE_PG else _SQLITE_SCHEMA
    for stmt in schema.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                cur.execute(stmt)
            except Exception:
                pass  # table/index already exists

    # seed super admin
    email    = os.getenv("SUPER_ADMIN_EMAIL",    "admin@university.edu.eg")
    password = os.getenv("SUPER_ADMIN_PASSWORD")
    if not password:
        password = "Admin@2026!"
        print("[WARNING] SUPER_ADMIN_PASSWORD is not set in environment. Falling back to default password.")
    name     = "مدير النظام"

    p = ph()
    if USE_PG:
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
    else:
        cur.execute("SELECT id FROM users WHERE email=?", (email,))
    if not cur.fetchone():
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        if USE_PG:
            cur.execute(
                "INSERT INTO users (email,password_hash,full_name,role,is_active,email_verified) VALUES (%s,%s,%s,%s,%s,%s)",
                (email, hashed, name, "superadmin", True, True)
            )
        else:
            cur.execute(
                "INSERT INTO users (email,password_hash,full_name,role,is_active,email_verified) VALUES (?,?,?,?,?,?)",
                (email, hashed, name, "superadmin", 1, 1)
            )

    conn.commit()
    conn.close()
    print(f"DB initialised ({'PostgreSQL' if USE_PG else 'SQLite'})")


# ── audit helper ───────────────────────────────────────────────────────────
def log_action(user_id, action, target=None, detail=None, ip=None):
    try:
        conn = get_db()
        cur  = conn.cursor()
        if USE_PG:
            cur.execute(
                "INSERT INTO audit_log (user_id,action,target,detail,ip) VALUES (%s,%s,%s,%s,%s)",
                (user_id, action, target, detail, ip)
            )
        else:
            cur.execute(
                "INSERT INTO audit_log (user_id,action,target,detail,ip) VALUES (?,?,?,?,?)",
                (user_id, action, target, detail, ip)
            )
        conn.commit()
        conn.close()
    except Exception:
        pass