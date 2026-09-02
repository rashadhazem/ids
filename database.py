"""
database.py  –  Dual-backend DB layer with automatic local SQLite fallback
  • SQLite  when DATABASE_URL is empty or when PostgreSQL fails/times out (local dev / offline)
  • PostgreSQL  when DATABASE_URL is reachable (production)
"""
import os, sqlite3, logging, re, requests
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

logger = logging.getLogger(__name__)

# ── helpers ────────────────────────────────────────────────────────────────
DB_URL      = os.getenv("DATABASE_URL", "")
USE_PG      = bool(DB_URL) and HAS_PG
PG_TIMEOUT  = int(os.getenv("PG_CONNECT_TIMEOUT", "3"))
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


def is_use_pg() -> bool:
    global USE_PG
    return bool(USE_PG and HAS_PG)


def set_use_pg(val: bool):
    global USE_PG
    USE_PG = bool(val)


def _connect_sqlite():
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_sqlite_init_done = False


def _ensure_sqlite_initialized():
    global _sqlite_init_done
    if _sqlite_init_done:
        return
    try:
        conn = _connect_sqlite()
        cur  = conn.cursor()
        migrations = [
            "ALTER TABLE users ADD COLUMN student_id TEXT",
        ]
        for m in migrations:
            try:
                cur.execute(m)
            except Exception:
                pass
        conn.commit()

        for stmt in _SQLITE_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass

        email    = os.getenv("SUPER_ADMIN_EMAIL", "admin@university.edu.eg")
        password = os.getenv("SUPER_ADMIN_PASSWORD") or "Admin@2026!"
        name     = "مدير النظام"

        cur.execute("SELECT id FROM users WHERE email=?", (email,))
        if not cur.fetchone():
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users (email,password_hash,full_name,role,is_active,email_verified) VALUES (?,?,?,?,?,?)",
                (email, hashed, name, "superadmin", 1, 1)
            )
            conn.commit()
        conn.close()
        _sqlite_init_done = True
        print(f"[INFO] Local SQLite database successfully initialized: {SQLITE_PATH}")
    except Exception as e:
        print(f"[ERROR] Failed to initialize SQLite database: {e}")


# ── Neon HTTP Adapter (Bypasses blocked port 5432 via HTTPS port 443) ───────
class NeonHTTPCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self._rows = []
        self._idx = 0
        self.rowcount = -1

    def execute(self, sql, params=None):
        count = 0
        def repl(m):
            nonlocal count
            count += 1
            return f"${count}"
        neon_sql = re.sub(r"%s", repl, sql)
        payload = {"query": neon_sql}
        if params:
            payload["params"] = list(params)
        res = self.conn.session.post(self.conn.endpoint, headers=self.conn.headers, json=payload, timeout=15)
        if res.status_code == 200:
            data = res.json()
            rows = data.get("rows", [])
            fields = data.get("fields", [])
            int_types = {20, 21, 23}      # int8, int2, int4
            float_types = {700, 701, 1700} # float4, float8, numeric
            if fields and rows:
                int_cols = [f["name"] for f in fields if f.get("dataTypeID") in int_types]
                float_cols = [f["name"] for f in fields if f.get("dataTypeID") in float_types]
                for r in rows:
                    for col in int_cols:
                        v = r.get(col)
                        if v is not None and not isinstance(v, int):
                            try:
                                r[col] = int(v)
                            except (ValueError, TypeError):
                                pass
                    for col in float_cols:
                        v = r.get(col)
                        if v is not None and not isinstance(v, (int, float)):
                            try:
                                r[col] = float(v)
                            except (ValueError, TypeError):
                                pass
            self._rows = rows
            self.rowcount = data.get("rowCount", len(self._rows))
            self._idx = 0
            self.description = [type("ColDesc", (), {"name": f["name"]})() for f in fields]
        else:
            raise Exception(f"Neon Query Error ({res.status_code}): {res.text}")

    def fetchone(self):
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rows

    def close(self):
        pass


class NeonHTTPConnection:
    _shared_session = None

    def __init__(self, db_url):
        host_part = db_url.split("@")[1].split("/")[0]
        self.endpoint = f"https://{host_part}/sql"
        self.headers = {
            "Neon-Connection-String": db_url,
            "Content-Type": "application/json"
        }
        if NeonHTTPConnection._shared_session is None:
            NeonHTTPConnection._shared_session = requests.Session()
        self.session = NeonHTTPConnection._shared_session

    def cursor(self):
        return NeonHTTPCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# Global flag to avoid waiting for 15s timeout on every request if port 5432 is blocked
_PG_PORT_BLOCKED = os.getenv("USE_NEON_HTTP", "").strip().lower() in ("true", "1", "yes")


# ── connection factory ─────────────────────────────────────────────────────
def get_db(force_sqlite: bool = False):
    global USE_PG, _PG_PORT_BLOCKED
    if force_sqlite:
        USE_PG = False
        _ensure_sqlite_initialized()
        return _connect_sqlite()

    if USE_PG:
        # If we know port 5432 is blocked (or configured to use HTTPS), use Neon HTTP directly
        if _PG_PORT_BLOCKED and "neon.tech" in DB_URL.lower():
            try:
                return NeonHTTPConnection(DB_URL)
            except Exception as e:
                print(f"[WARNING] Neon HTTP connection failed: {e}")

        try:
            conn = psycopg2.connect(
                DB_URL,
                connect_timeout=PG_TIMEOUT,
                cursor_factory=psycopg2.extras.RealDictCursor
            )
            return conn
        except Exception as e:
            # If standard TCP port 5432 failed (e.g. firewall/ISP blocked), bypass via Neon HTTPS port 443!
            if "neon.tech" in DB_URL.lower():
                try:
                    conn = NeonHTTPConnection(DB_URL)
                    cur = conn.cursor()
                    cur.execute("SELECT 1")
                    _PG_PORT_BLOCKED = True
                    print("[INFO] Bypassing blocked port 5432: Connected to Neon PostgreSQL via HTTPS (Port 443)!")
                    return conn
                except Exception as he:
                    print(f"[WARNING] Neon HTTPS fallback failed: {he}")

            print(f"[WARNING] PostgreSQL connection failed: {e}")
            print(f"[INFO] Automatically falling back to local SQLite database: {SQLITE_PATH}")
            USE_PG = False
            _ensure_sqlite_initialized()
            return _connect_sqlite()
    else:
        return _connect_sqlite()


def placeholder(n: int) -> str:
    """Return %s for PG or ? for SQLite, repeated n times."""
    ch = "%s" if is_use_pg() else "?"
    return ", ".join([ch] * n)


def ph() -> str:
    """Single placeholder."""
    return "%s" if is_use_pg() else "?"


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
    global USE_PG, _sqlite_init_done
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    conn = get_db()
    cur  = conn.cursor()

    if is_use_pg():
        # Fast check: if users table already exists, skip redundant DDL statements
        try:
            cur.execute("SELECT 1 FROM users LIMIT 1")
            print("[INFO] PostgreSQL database connected and verified.")
            return
        except Exception:
            pass

        # ── Migration FIRST: add missing columns to existing tables ──
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS student_id TEXT UNIQUE")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_student ON users(student_id)")
        except Exception:
            pass
        conn.commit()

        # ── Create tables + indexes ──
        for stmt in _PG_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass

        # seed super admin
        email    = os.getenv("SUPER_ADMIN_EMAIL", "admin@university.edu.eg")
        password = os.getenv("SUPER_ADMIN_PASSWORD")
        if not password:
            password = "Admin@2026!"
            print("[WARNING] SUPER_ADMIN_PASSWORD is not set in environment. Falling back to default password.")
        name     = "مدير النظام"

        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        if not cur.fetchone():
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users (email,password_hash,full_name,role,is_active,email_verified) VALUES (%s,%s,%s,%s,%s,%s)",
                (email, hashed, name, "superadmin", True, True)
            )

        conn.commit()
        conn.close()
        print("DB initialised (PostgreSQL)")
    else:
        # SQLite migrations
        migrations = [
            "ALTER TABLE users ADD COLUMN student_id TEXT",
        ]
        for m in migrations:
            try:
                cur.execute(m)
            except Exception:
                pass
        conn.commit()

        # SQLite schema
        for stmt in _SQLITE_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass

        # seed super admin
        email    = os.getenv("SUPER_ADMIN_EMAIL", "admin@university.edu.eg")
        password = os.getenv("SUPER_ADMIN_PASSWORD")
        if not password:
            password = "Admin@2026!"
            print("[WARNING] SUPER_ADMIN_PASSWORD is not set in environment. Falling back to default password.")
        name     = "مدير النظام"

        cur.execute("SELECT id FROM users WHERE email=?", (email,))
        if not cur.fetchone():
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users (email,password_hash,full_name,role,is_active,email_verified) VALUES (?,?,?,?,?,?)",
                (email, hashed, name, "superadmin", 1, 1)
            )

        conn.commit()
        conn.close()
        _sqlite_init_done = True
        print("DB initialised (SQLite - Local storage active)")


# ── audit helper ───────────────────────────────────────────────────────────
def log_action(user_id, action, target=None, detail=None, ip=None):
    try:
        conn = get_db()
        cur  = conn.cursor()
        if is_use_pg():
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