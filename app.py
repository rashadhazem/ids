import os, re, secrets, io, zipfile, base64
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
from dotenv import load_dotenv
from flask import (Flask, request, jsonify, session, redirect,
                   url_for, render_template, send_file, abort, g)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mail import Mail, Message
from flask_jwt_extended import (JWTManager, create_access_token,
                                 jwt_required, get_jwt_identity)
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from werkzeug.utils import secure_filename
import tempfile

# ── bootstrap ──────────────────────────────────────────────────────────────
load_dotenv()

from database import get_db, init_db, COLLEGES, ROLES, log_action, ph, is_use_pg
from image_processor import (detect_faces, apply_edits, face_detected, save_image,
                              archive_old_image, move_student_images_locally,
                              TARGET_W, TARGET_H, validate_single_person,
                              process_and_validate_photo)
try:
    from gdrive_helper import upload_to_gdrive, archive_in_gdrive, upload_backup_to_gdrive, move_student_in_gdrive
except ImportError:
    upload_to_gdrive = lambda *a, **kw: False
    archive_in_gdrive = lambda *a, **kw: False
    upload_backup_to_gdrive = lambda *a, **kw: False
    move_student_in_gdrive = lambda *a, **kw: False

app = Flask(__name__)
# Enable ProxyFix behind a reverse proxy (e.g. Nginx)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# Initialize CSRF Protection
csrf = CSRFProtect(app)

# Load configuration securely
secret_key_env = os.getenv("SECRET_KEY")
if not secret_key_env:
    app.logger.warning("[SECURITY WARNING] SECRET_KEY not set. Using temporary random key.")
    secret_key_env = secrets.token_hex(32)
app.secret_key = secret_key_env

jwt_secret_env = os.getenv("JWT_SECRET_KEY")
if not jwt_secret_env:
    app.logger.warning("[SECURITY WARNING] JWT_SECRET_KEY not set. Using temporary random key.")
    jwt_secret_env = secrets.token_hex(32)
app.config["JWT_SECRET_KEY"]           = jwt_secret_env

app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=8)
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Lax"
app.config["SESSION_COOKIE_SECURE"]    = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["MAX_CONTENT_LENGTH"]       = int(os.getenv("MAX_CONTENT_LENGTH", 5*1024*1024))
app.config["WTF_CSRF_TIME_LIMIT"]      = None  # CSRF tokens remain valid for the full duration of session
app.config["WTF_CSRF_CHECK_DEFAULT"]   = True
mail_port = int(os.getenv("MAIL_PORT", 587))
use_ssl_env = os.getenv("MAIL_USE_SSL")
use_tls_env = os.getenv("MAIL_USE_TLS")

app.config["MAIL_SERVER"]              = os.getenv("MAIL_SERVER",  "smtp.gmail.com")
app.config["MAIL_PORT"]                = mail_port
app.config["MAIL_USE_SSL"]             = use_ssl_env.lower() == "true" if use_ssl_env is not None else (mail_port == 465)
app.config["MAIL_USE_TLS"]             = use_tls_env.lower() == "true" if use_tls_env is not None else (mail_port == 587 and not (use_ssl_env and use_ssl_env.lower() == "true"))

# SMTP Credentials from Environment Variables (V-001)
app.config["MAIL_USERNAME"]            = os.getenv("MAIL_USERNAME")
mail_pw = os.getenv("MAIL_PASSWORD", "")
app.config["MAIL_PASSWORD"]            = mail_pw.replace(" ", "") if mail_pw else None
app.config["MAIL_DEFAULT_SENDER"]      = os.getenv("MAIL_DEFAULT_SENDER") or os.getenv("MAIL_USERNAME")

UPLOAD_FOLDER        = os.path.join(app.root_path, "static", "uploads")
STATIC_ROOT          = os.path.join(app.root_path, "static")
UNIVERSITY_DOMAIN    = os.getenv("UNIVERSITY_EMAIL_DOMAIN", "bua.edu.eg").split("#")[0].strip()
CURRENT_YEAR         = datetime.now().year
YEAR_RANGE           = list(range(CURRENT_YEAR, CURRENT_YEAR - 10, -1))

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.after_request
def add_security_headers(response):
    response.headers["X-Frame-Options"] = "SAMEORIGIN"  # Clickjacking mitigation (V-008)
    response.headers["X-Content-Type-Options"] = "nosniff"  # MIME sniffing prevention (V-024)
    # Content Security Policy (V-022)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: https://res.cloudinary.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "frame-ancestors 'none';"
    )
    # Strict Transport Security (HSTS) (V-023)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

@app.route("/api/csrf-token", methods=["GET"])
def get_csrf_token_endpoint():
    from flask_wtf.csrf import generate_csrf
    return jsonify(success=True, csrf_token=generate_csrf())

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    app.logger.warning(f"[CSRF Error] {e.description} on {request.method} {request.path}")
    if (request.is_json or 
        request.headers.get("X-Requested-With") == "XMLHttpRequest" or 
        "application/json" in request.headers.get("Accept", "") or
        request.headers.get("X-CSRFToken") or
        request.path in ["/register", "/student/register", "/auth/change-password"] or
        request.path.startswith(("/admin/", "/student/", "/api/", "/auth/"))):
        return jsonify(
            success=False,
            message="انتهت صلاحية رمز الأمان أو جلسة العمل. يرجى تحديث الصفحة وإعادة المحاولة.",
            csrf_error=True
        ), 400
    return render_template("auth_message.html",
                           title="انتهت صلاحية الجلسة",
                           message="انتهت صلاحية رمز الأمان الخاص بك. يرجى تحديث الصفحة والمحاولة مجدداً."), 400

mail    = Mail(app)
jwt     = JWTManager(app)
# Specific sensitive routes (login, register, reset pw) are protected by explicit @limiter.limit
# Global default is kept open so thousands of concurrent students viewing pages are never blocked
def _concurrency_safe_limiter_key():
    # If user is in an active session, limit by user/student ID so hundreds of students on the same NAT/WiFi IP are not blocked!
    try:
        if session:
            sid = session.get("student_id") or session.get("user_id") or session.get("email")
            if sid:
                return f"sess_{sid}"
    except Exception:
        pass
    return get_remote_address()

limiter = Limiter(
    key_func=_concurrency_safe_limiter_key, app=app,
    default_limits=[],
    storage_uri=os.getenv("LIMITER_STORAGE_URI", "memory://"),
)

ARABIC_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# ── helpers ────────────────────────────────────────────────────────────────

def to_eng(s): return s.translate(ARABIC_MAP)

def validate_university_email(email: str) -> bool:
    email = email.strip().lower()
    valid_domains = {"bua.edu.eg", UNIVERSITY_DOMAIN.lower()}
    domain_part = email.split("@")[-1] if "@" in email else ""
    return domain_part in valid_domains

def validate_full_name(name: str) -> tuple[bool, str]:
    name = (name or "").strip()
    if not name:
        return False, "يرجى إدخال الاسم"
    if not re.fullmatch(r"^[\u0621-\u064A\u0671\s]+$", name):
        return False, "الاسم يجب أن يكون باللغة العربية فقط (بدون أرقام أو حروف إنجليزية)"
    parts = [p for p in name.split() if p]
    if len(parts) < 4:
        return False, "الاسم يجب أن يكون رباعياً على الأقل"
    return True, ""

def validate_student_id(year: str, code: str) -> tuple[bool, str]:
    if not re.fullmatch(r"\d{4}", year):   return False, "السنة يجب أن تكون 4 أرقام"
    if not (CURRENT_YEAR - 10 <= int(year) <= CURRENT_YEAR):
        return False, f"سنة القيد غير صالحة. يجب أن تكون بين {CURRENT_YEAR - 10} و {CURRENT_YEAR}"
    if not re.fullmatch(r"\d{6}|\d{8}", code): return False, "الكود يجب أن يكون 6 أو 8 أرقام"
    return True, ""

def extract_student_info_from_email(email: str) -> dict:
    """
    Extract student_id, year, and code from university email.
    e.g. abdulrahman.2023006972@bua.edu.eg ->
    {'student_id': '2023006972', 'year': '2023', 'code': '006972'}
    """
    if not email:
        return {}
    email_prefix = email.split("@")[0].strip()
    # Match pattern: name.YYYYxxxxxx or name_YYYYxxxxxx (year 4 digits + code 6-8 digits)
    match = re.search(r'(?:^|[\._\-])(\d{4})(\d{6,8})$', email_prefix)
    if match:
        year = match.group(1)
        code = match.group(2)
        return {
            "student_id": year + code,
            "year": year,
            "code": code
        }
    # Fallback: any 10-12 digits at the end
    match2 = re.search(r'(?:^|[\._\-])(\d{10,12})$', email_prefix)
    if match2:
        sid = match2.group(1)
        return {
            "student_id": sid,
            "year": sid[:4],
            "code": sid[4:]
        }
    return {}

def extract_student_id_from_email(email: str) -> str:
    """Extract student ID from email like abdulrahman.2023006972@bua.edu.eg"""
    info = extract_student_info_from_email(email)
    return info.get("student_id")

def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_pw(pw: str, hashed: str, plain: str = None) -> bool:
    """Verify password: checks plain-text match first, falls back to bcrypt hash."""
    if plain is not None and plain != "":
        if pw == plain:
            return True
    if hashed:
        try:
            return bcrypt.checkpw(pw.encode(), hashed.encode())
        except Exception:
            return False
    return False

def send_email(to: str, subject: str, html: str):
    if not app.config.get("MAIL_USERNAME") or not app.config.get("MAIL_PASSWORD"):
        app.logger.warning("📧 MAIL not configured – skipping email to %s | Subject: %s", to, subject)
        app.logger.warning("📧 To fix: set MAIL_USERNAME + MAIL_PASSWORD in .env (use Gmail App Password)")
        return
    try:
        msg = Message(subject, recipients=[to], html=html)
        mail.send(msg)
        app.logger.info("📧 Email sent to %s", to)
    except Exception as e:
        app.logger.error("📧 Email error to %s: %s", to, e)

def _row_to_dict(row) -> dict:
    if row is None: return {}
    if is_use_pg(): return dict(row)
    return dict(zip(row.keys(), row))

# ── auth decorators ────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def inner(*a, **kw):
        if not session.get("user_id"):
            return redirect(url_for("auth_login"))
        return f(*a, **kw)
    return inner

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def inner(*a, **kw):
            if not session.get("user_id"):
                return redirect(url_for("auth_login"))
            if session.get("role") not in roles:
                abort(403)
            return f(*a, **kw)
        return inner
    return decorator

def _current_user():
    uid = session.get("user_id")
    if not uid: return None
    db  = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM users WHERE id={ph()}", (uid,))
    row = _row_to_dict(cur.fetchone()); db.close()
    return row

# ── email templates ────────────────────────────────────────────────────────

def _email_verify_html(name, link):
    return f"""
<div dir="rtl" style="font-family:Cairo,Arial;max-width:520px;margin:auto">
  <div style="background:#0d1f3c;padding:28px;border-radius:14px 14px 0 0;text-align:center">
    <h2 style="color:#e8b84b;margin:0">تأكيد البريد الإلكتروني</h2>
  </div>
  <div style="background:#f0f4f9;padding:28px;border-radius:0 0 14px 14px">
    <p>أهلاً <strong>{name}</strong>،</p>
    <p>انقر على الزر أدناه لتفعيل حسابك:</p>
    <a href="{link}" style="display:inline-block;background:#0d1f3c;color:#e8b84b;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;margin:16px 0">تفعيل الحساب</a>
    <p style="color:#888;font-size:.85rem">الرابط صالح لمدة 24 ساعة</p>
  </div>
</div>"""

def _email_reset_html(name, link):
    return f"""
<div dir="rtl" style="font-family:Cairo,Arial;max-width:520px;margin:auto">
  <div style="background:#c53030;padding:28px;border-radius:14px 14px 0 0;text-align:center">
    <h2 style="color:#fff;margin:0">إعادة تعيين كلمة المرور</h2>
  </div>
  <div style="background:#f0f4f9;padding:28px;border-radius:0 0 14px 14px">
    <p>أهلاً <strong>{name}</strong>،</p>
    <p>انقر على الزر أدناه لإعادة تعيين كلمة مرورك:</p>
    <a href="{link}" style="display:inline-block;background:#c53030;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;margin:16px 0">إعادة التعيين</a>
    <p style="color:#888;font-size:.85rem">الرابط صالح لمدة ساعة واحدة فقط. إذا لم تطلب ذلك تجاهل هذا البريد.</p>
  </div>
</div>"""

def _email_registered_html(name, student_id, login_email, password, card_link):
    return f"""
<div dir="rtl" style="font-family:Cairo,Arial;max-width:520px;margin:auto">
  <div style="background:#0d1f3c;padding:28px;border-radius:14px 14px 0 0;text-align:center">
    <h2 style="color:#e8b84b;margin:0">&#127891; تم تسجيلك بنجاح</h2>
  </div>
  <div style="background:#f0f4f9;padding:28px;border-radius:0 0 14px 14px">
    <p>أهلاً <strong>{name}</strong>،</p>
    <p>تم تسجيلك في النظام بنجاح. رقمك الجامعي هو:</p>
    <div style="background:#0d1f3c;color:#e8b84b;font-family:monospace;font-size:1.4rem;font-weight:700;padding:14px;border-radius:8px;text-align:center;letter-spacing:3px;margin:16px 0">{student_id}</div>

    <div style="background:#fff;border:1px solid #dce3ef;border-radius:10px;padding:16px;margin:14px 0">
      <p style="font-weight:700;color:#1a2744;margin-bottom:8px">بيانات تسجيل الدخول:</p>
      <p style="font-size:.88rem;color:#444;margin-bottom:5px">البريد الجامعي / اسم المستخدم: <strong style="direction:ltr;display:inline-block">{login_email}</strong></p>
      <p style="font-size:.88rem;color:#444">كلمة المرور (كود الطالب): <strong style="font-family:monospace;letter-spacing:1px">{password}</strong></p>
    </div>

    <a href="{card_link}" style="display:inline-block;background:#1a3a6b;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700">عرض بطاقة الهوية</a>
  </div>
</div>"""

# ══════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════

@app.route("/auth/login", methods=["GET","POST"])
@limiter.limit(lambda: os.getenv("LIMIT_AUTH_LOGIN", "500 per hour"), methods=["POST"])
def auth_login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    error = None
    unverified_email = None
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pw    = request.form.get("password","")
        db    = get_db(); cur = db.cursor()
        cur.execute(f"SELECT * FROM users WHERE email={ph()} OR student_id={ph()}", (email, email))
        u = _row_to_dict(cur.fetchone()); db.close()
        if not u:
            error = "البريد الإلكتروني غير مسجل"
        elif not u.get("is_active"):
            error = "الحساب موقوف. تواصل مع مدير النظام"
        elif not u.get("email_verified"):
            error = "يرجى تفعيل بريدك الإلكتروني أولاً"
            unverified_email = email
        elif not check_pw(pw, u["password_hash"], u.get("password_plain")):
            error = "كلمة المرور غير صحيحة"
        else:
            csrf_token_val = session.get("csrf_token")
            session.clear()
            if csrf_token_val:
                session["csrf_token"] = csrf_token_val
            session["user_id"]    = u["id"]
            session["user_name"]  = u["full_name"]
            session["role"]       = u["role"]
            session["email"]      = u["email"]
            session["college"]    = u.get("college") or ""
            session["student_id"] = u.get("student_id") or ""
            log_action(u["id"], "LOGIN", ip=request.remote_addr)
            # Students → check if registered, go to card or self-register
            if u["role"] == "student":
                sid = u.get("student_id") or ""
                if sid:
                    db2  = get_db(); cur2 = db2.cursor()
                    cur2.execute(f"SELECT student_id FROM students WHERE student_id={ph()}", (sid,))
                    exists = cur2.fetchone(); db2.close()
                    if exists:
                        return redirect(url_for("student_card", student_id=sid))
                # Not registered yet → self-registration page
                return redirect(url_for("student_self_register"))
            return redirect(url_for("dashboard"))
    return render_template("auth_login.html", error=error,
                           unverified_email=unverified_email,
                           domain=UNIVERSITY_DOMAIN)


@app.route("/student/login", methods=["GET","POST"])
@limiter.limit(lambda: os.getenv("LIMIT_STUDENT_LOGIN", "3000 per hour"), methods=["POST"])
def student_login():
    """Dedicated login page for students."""
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    error = None
    unverified_email = None
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pw    = request.form.get("password","")
        db    = get_db(); cur = db.cursor()
        cur.execute(f"SELECT * FROM users WHERE email={ph()} OR student_id={ph()}", (email, email))
        u = _row_to_dict(cur.fetchone()); db.close()
        if not u:
            error = "البريد الإلكتروني غير مسجل"
        elif u.get("role") != "student":
            error = "هذه الصفحة مخصصة للطلاب فقط"
        elif not u.get("is_active"):
            error = "الحساب موقوف. تواصل مع مدير النظام"
        elif not u.get("email_verified"):
            error = "يرجى تفعيل بريدك الإلكتروني أولاً"
            unverified_email = email
        elif not check_pw(pw, u["password_hash"], u.get("password_plain")):
            error = "كلمة المرور غير صحيحة"
        else:
            csrf_token_val = session.get("csrf_token")
            session.clear()
            if csrf_token_val:
                session["csrf_token"] = csrf_token_val
            session["user_id"]    = u["id"]
            session["user_name"]  = u["full_name"]
            session["role"]       = u["role"]
            session["email"]      = u["email"]
            session["college"]    = u.get("college") or ""
            session["student_id"] = u.get("student_id") or ""
            log_action(u["id"], "LOGIN", ip=request.remote_addr)
            # Students → check if registered, go to card or self-register
            sid = u.get("student_id") or ""
            if sid:
                db2  = get_db(); cur2 = db2.cursor()
                cur2.execute(f"SELECT student_id FROM students WHERE student_id={ph()}", (sid,))
                exists = cur2.fetchone(); db2.close()
                if exists:
                    return redirect(url_for("student_card", student_id=sid))
            # Not registered yet → self-registration page
            return redirect(url_for("student_self_register"))
    return render_template("student_login.html", error=error,
                           unverified_email=unverified_email,
                           domain=UNIVERSITY_DOMAIN)


@app.route("/auth/register", methods=["GET","POST"])
@limiter.limit(lambda: os.getenv("LIMIT_AUTH_REGISTER", "30 per hour"))
def auth_register():
    """Public self-registration — always assigns 'student' role.
    Admins/staff accounts are created by superadmin only."""
    error = None
    if request.method == "POST":
        email     = request.form.get("email","").strip().lower()
        pw        = request.form.get("password","")
        pw2       = request.form.get("password2","")
        full_name = request.form.get("full_name","").strip()

        if not validate_university_email(email):
            error = f"يجب استخدام إيمبيل الجامعة (@{UNIVERSITY_DOMAIN})"
        elif not full_name:
            error = "يرجى إدخال الاسم"
        elif not re.fullmatch(r"^[\u0621-\u064A\u0671\s]+$", full_name):
            error = "الاسم يجب أن يكون باللغة العربية فقط (بدون أرقام أو حروف إنجليزية)"
        elif len(full_name.split()) < 2:
            error = "يرجى إدخال الاسم كاملاً (الاسم الثنائي أو الثلاثي على الأقل)"
        elif len(pw) < 8:
            error = "كلمة المرور يجب أن تكون 8 أحرف على الأقل"
        elif pw != pw2:
            error = "كلمتا المرور غير متطابقتين"
        else:
            # Extract student_id from email prefix (e.g. AbdulRahman.2023006972@bua.edu.eg)
            sid = extract_student_id_from_email(email)

            db  = get_db(); cur = db.cursor()
            cur.execute(f"SELECT id FROM users WHERE email={ph()}", (email,))
            if cur.fetchone():
                db.close()
                error = "هذا البريد مسجل مسبقاً"
            else:
                token  = secrets.token_urlsafe(32)
                hashed = hash_pw(pw)
                # Always student role — admin creates staff/admin accounts separately
                cur.execute(
                    f"INSERT INTO users (email,password_hash,password_plain,full_name,role,student_id,verify_token) VALUES ({','.join([ph()]*7)})",
                    (email, hashed, pw, full_name, "student", sid, token)
                )
                db.commit(); db.close()
                link = url_for("auth_verify", token=token, _external=True)
                send_email(email, "تفعيل حساب نظام التسجيل", _email_verify_html(full_name, link))
                return render_template("auth_register.html",
                    success="تم إنشاء حسابك! تحقق من بريدك لتفعيل الحساب.",
                    registered_email=email,
                    domain=UNIVERSITY_DOMAIN)
    return render_template("auth_register.html", error=error, domain=UNIVERSITY_DOMAIN)


@app.route("/auth/verify/<token>")
def auth_verify(token):
    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM users WHERE verify_token={ph()}", (token,))
    u = _row_to_dict(cur.fetchone())
    if not u:
        db.close()
        return render_template("auth_message.html",
            title="رابط غير صالح", msg="رابط التفعيل منتهي أو غير صحيح.", type="error")
    if is_use_pg():
        cur.execute("UPDATE users SET email_verified=TRUE, verify_token=NULL WHERE id=%s", (u["id"],))
    else:
        cur.execute("UPDATE users SET email_verified=1, verify_token=NULL WHERE id=?", (u["id"],))
    db.commit(); db.close()
    log_action(u["id"], "EMAIL_VERIFIED", ip=request.remote_addr)
    return render_template("auth_message.html",
        title="تم التفعيل ✓", msg="تم تفعيل حسابك بنجاح. يمكنك الآن تسجيل الدخول.", type="success")


@app.route("/auth/resend-verification", methods=["GET", "POST"])
@limiter.limit(lambda: os.getenv("LIMIT_AUTH_RESEND", "15 per hour"))
def auth_resend_verification():
    """Resend email verification activation link."""
    msg = None
    msg_type = "info"
    email_val = request.args.get("email", "").strip().lower()

    if request.method == "POST":
        email_val = request.form.get("email", "").strip().lower()
        if not email_val:
            msg = "يرجى إدخال البريد الإلكتروني"
            msg_type = "error"
        elif not validate_university_email(email_val):
            msg = f"يجب استخدام بريد الجامعة (@{UNIVERSITY_DOMAIN})"
            msg_type = "error"
        else:
            db  = get_db()
            cur = db.cursor()
            cur.execute(f"SELECT * FROM users WHERE email={ph()}", (email_val,))
            u = _row_to_dict(cur.fetchone())

            if u:
                if u.get("email_verified"):
                    msg = "هذا الحساب مفعّل بالفعل! يمكنك تسجيل الدخول مباشرة."
                    msg_type = "success"
                elif not u.get("is_active"):
                    msg = "هذا الحساب موقوف. يرجى مراجعة إدارة النظام."
                    msg_type = "error"
                else:
                    token = secrets.token_urlsafe(32)
                    cur.execute(
                        f"UPDATE users SET verify_token={ph()} WHERE id={ph()}",
                        (token, u["id"])
                    )
                    db.commit()
                    link = url_for("auth_verify", token=token, _external=True)
                    send_email(
                        email_val,
                        "تفعيل حساب نظام التسجيل",
                        _email_verify_html(u["full_name"], link)
                    )
                    msg = "تم إرسال رابط التفعيل الجديد بنجاح إلى بريدك الجامعي! يرجى التحقق من صندوق الوارد (أو مجلد Spam)."
                    msg_type = "success"
            else:
                msg = "إذا كان هذا البريد مسجلاً لدينا وغير مفعّل، فقد تم إرسال رابط التفعيل إليه."
                msg_type = "info"
            db.close()

    return render_template(
        "auth_resend.html",
        msg=msg,
        msg_type=msg_type,
        email_val=email_val,
        domain=UNIVERSITY_DOMAIN
    )


@app.route("/auth/forgot", methods=["GET","POST"])
@limiter.limit(lambda: os.getenv("LIMIT_AUTH_FORGOT", "10 per hour"))
def auth_forgot():
    msg = None
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        db    = get_db(); cur = db.cursor()
        cur.execute(f"SELECT * FROM users WHERE email={ph()}", (email,))
        u = _row_to_dict(cur.fetchone())
        if u:
            token   = secrets.token_urlsafe(32)
            expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            cur.execute(
                f"UPDATE users SET reset_token={ph()}, reset_expires={ph()} WHERE id={ph()}",
                (token, expires, u["id"])
            )
            db.commit()
            link = url_for("auth_reset", token=token, _external=True)
            send_email(email, "إعادة تعيين كلمة المرور", _email_reset_html(u["full_name"], link))
        db.close()
        msg = "إذا كان البريد مسجلاً ستصلك رسالة خلال دقائق"
    return render_template("auth_forgot.html", msg=msg)


@app.route("/auth/reset/<token>", methods=["GET","POST"])
def auth_reset(token):
    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM users WHERE reset_token={ph()}", (token,))
    u = _row_to_dict(cur.fetchone())
    if not u or (u.get("reset_expires") and
                 datetime.fromisoformat(str(u["reset_expires"])) < datetime.utcnow()):
        db.close()
        return render_template("auth_message.html",
            title="رابط منتهي", msg="رابط إعادة التعيين منتهي الصلاحية.", type="error")
    error = None
    if request.method == "POST":
        pw  = request.form.get("password","")
        pw2 = request.form.get("password2","")
        if len(pw) < 8: error = "كلمة المرور يجب أن تكون 8 أحرف على الأقل"
        elif pw != pw2:  error = "كلمتا المرور غير متطابقتين"
        else:
            hashed = hash_pw(pw)
            cur.execute(
                f"UPDATE users SET password_hash={ph()}, password_plain={ph()}, reset_token=NULL, reset_expires=NULL WHERE id={ph()}",
                (hashed, pw, u["id"])
            )
            db.commit(); db.close()
            log_action(u["id"], "PASSWORD_RESET", ip=request.remote_addr)
            return render_template("auth_message.html",
                title="تم تغيير كلمة المرور ✓",
                msg="يمكنك الآن تسجيل الدخول بكلمة المرور الجديدة.", type="success")
    db.close()
    return render_template("auth_reset.html", token=token, error=error)


@app.route("/auth/logout")
def auth_logout():
    uid = session.get("user_id")
    if uid: log_action(uid, "LOGOUT", ip=request.remote_addr)
    session.clear()
    return redirect(url_for("auth_login"))


@app.route("/auth/change-password", methods=["POST"])
@login_required
@limiter.limit(lambda: os.getenv("LIMIT_AUTH_CHANGE_PW", "15 per hour"))
def auth_change_password():
    """Allow any authenticated user (superadmin, admin, staff, student) to change their password."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(success=False, message="غير مصرح. يرجى تسجيل الدخول أولاً"), 401

    data = request.get_json() if request.is_json else request.form
    cur_pw  = (data.get("current_password") or "").strip()
    new_pw  = (data.get("new_password") or "").strip()
    new_pw2 = (data.get("confirm_password") or "").strip()

    if not cur_pw:
        return jsonify(success=False, message="يرجى إدخال كلمة المرور الحالية"), 400
    if not new_pw:
        return jsonify(success=False, message="يرجى إدخال كلمة المرور الجديدة"), 400
    if len(new_pw) < 8:
        return jsonify(success=False, message="كلمة المرور الجديدة يجب أن تكون 8 أحرف على الأقل"), 400
    if new_pw != new_pw2:
        return jsonify(success=False, message="كلمتا المرور الجديدتان غير متطابقتين"), 400
    if cur_pw == new_pw:
        return jsonify(success=False, message="كلمة المرور الجديدة يجب أن تختلف عن كلمة المرور الحالية"), 400

    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT password_hash, email FROM users WHERE id={ph()}", (uid,))
    u = _row_to_dict(cur.fetchone())
    if not u:
        db.close()
        return jsonify(success=False, message="المستخدم غير موجود"), 404

    if not check_pw(cur_pw, u.get("password_hash"), u.get("password_plain")):
        db.close()
        return jsonify(success=False, message="كلمة المرور الحالية غير صحيحة"), 400

    new_hashed = hash_pw(new_pw)
    cur.execute(f"UPDATE users SET password_hash={ph()}, password_plain={ph()} WHERE id={ph()}", (new_hashed, new_pw, uid))
    db.commit()
    db.close()

    log_action(uid, "CHANGE_PASSWORD", target=u["email"], detail="User changed password", ip=request.remote_addr)
    return jsonify(success=True, message="تم تغيير كلمة المرور بنجاح ✓")


# ══════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

@app.route("/")
@app.route("/dashboard")
@login_required
def dashboard():
    if session.get("role") == "student":
        sid = session.get("student_id", "")
        if sid:
            db = get_db(); cur = db.cursor()
            cur.execute(f"SELECT student_id FROM students WHERE student_id={ph()}", (sid,))
            exists = cur.fetchone(); db.close()
            if exists:
                return redirect(url_for("student_card", student_id=sid))
        return redirect(url_for("student_self_register"))

    db  = get_db(); cur = db.cursor()
    role = session.get("role")
    user_college = session.get("college")

    if role in ("superadmin", "staff"):
        cur.execute("SELECT COUNT(*) AS c FROM students")
        total = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)
        cur.execute("SELECT COUNT(DISTINCT year) AS c FROM students")
        years = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)
        cur.execute("SELECT COUNT(DISTINCT college) AS c FROM students")
        colleges_n = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE role IN ('admin', 'staff')")
        supervisors_n = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)

        # College distribution counts
        cur.execute("SELECT college, COUNT(*) as count FROM students GROUP BY college ORDER BY count DESC")
        college_counts = [_row_to_dict(r) for r in cur.fetchall()]

        # Latest students across university
        cur.execute("SELECT student_id, full_name, college, year, image_path, created_at FROM students ORDER BY created_at DESC LIMIT 8")
        recent_students = [_row_to_dict(r) for r in cur.fetchall()]

        # Recent audit:
        # Superadmin sees all activity across all users, staff, and students
        # Student Affairs staff only sees student-related activities
        if role == "superadmin":
            cur.execute("""
                SELECT a.action, a.target, a.created_at,
                       COALESCE(s.full_name, u.full_name, 'نظام') AS full_name,
                       COALESCE(s.student_id, u.student_id, a.target) AS student_id,
                       a.detail
                FROM audit_log a
                LEFT JOIN users u ON a.user_id=u.id
                LEFT JOIN students s ON (a.target = s.student_id OR (u.student_id IS NOT NULL AND u.student_id = s.student_id))
                ORDER BY a.created_at DESC LIMIT 10
            """)
        else:
            cur.execute("""
                SELECT a.action, a.target, a.created_at,
                       COALESCE(s.full_name, u.full_name, 'طالب') AS full_name,
                       COALESCE(s.student_id, u.student_id, a.target) AS student_id,
                       a.detail
                FROM audit_log a
                LEFT JOIN users u ON a.user_id=u.id
                LEFT JOIN students s ON (a.target = s.student_id OR (u.student_id IS NOT NULL AND u.student_id = s.student_id))
                WHERE a.action IN ('REGISTER_STUDENT', 'UPDATE_PHOTO', 'DELETE_STUDENT', 'EDIT_STUDENT', 'BULK_IMPORT_STUDENT', 'STUDENT_SELF_REGISTER')
                   OR u.role = 'student'
                ORDER BY a.created_at DESC LIMIT 10
            """)
        audit = [_row_to_dict(r) for r in cur.fetchall()]
        for a_row in audit:
            if a_row.get("created_at"):
                ca = str(a_row["created_at"])
                a_row["created_at_fmt"] = ca[:19].replace("T", " ")
        db.close()

        return render_template("dashboard.html",
            total=total, years=years, colleges_n=colleges_n,
            supervisors_n=supervisors_n, college_counts=college_counts,
            recent_students=recent_students, audit=audit,
            role=role, user_name=session.get("user_name"),
            user_college=None, colleges=COLLEGES, all_years=YEAR_RANGE)
    else:
        # College Supervisor Dashboard
        cur.execute(f"SELECT COUNT(*) AS c FROM students WHERE college={ph()}", (user_college,))
        total = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)
        cur.execute(f"SELECT COUNT(DISTINCT year) AS c FROM students WHERE college={ph()}", (user_college,))
        years = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)

        # Latest students in this specific college
        cur.execute(f"SELECT student_id, full_name, year, image_path, created_at FROM students WHERE college={ph()} ORDER BY created_at DESC LIMIT 10", (user_college,))
        recent_students = [_row_to_dict(r) for r in cur.fetchall()]

        # Recent audit for college supervisor: strictly activities of students in their assigned college
        cur.execute(f"""
            SELECT a.action, a.target, a.created_at,
                   COALESCE(s.full_name, u.full_name, 'طالب') AS full_name,
                   COALESCE(s.student_id, u.student_id, a.target) AS student_id,
                   a.detail
            FROM audit_log a
            LEFT JOIN users u ON a.user_id = u.id
            LEFT JOIN students s ON (a.target = s.student_id OR (u.student_id IS NOT NULL AND u.student_id = s.student_id))
            WHERE (
                (u.role = 'student' AND (u.college = {ph()} OR s.college = {ph()}))
                OR (a.target IN (SELECT student_id FROM students WHERE college = {ph()}))
                OR (a.target IN (SELECT student_id FROM users WHERE college = {ph()} AND role = 'student'))
                OR (a.target IN (SELECT email FROM users WHERE college = {ph()} AND role = 'student'))
                OR (a.target IN (SELECT h.old_student_id FROM student_id_history h JOIN students st ON h.new_student_id = st.student_id WHERE st.college = {ph()}))
            )
            ORDER BY a.created_at DESC LIMIT 10
        """, (user_college, user_college, user_college, user_college, user_college, user_college))
        audit = [_row_to_dict(r) for r in cur.fetchall()]
        for a_row in audit:
            if a_row.get("created_at"):
                ca = str(a_row["created_at"])
                a_row["created_at_fmt"] = ca[:19].replace("T", " ")
        db.close()

        return render_template("dashboard.html",
            total=total, years=years, colleges_n=1,
            user_college=user_college,
            recent_students=recent_students, audit=audit,
            role=role, user_name=session.get("user_name"),
            colleges=COLLEGES, all_years=YEAR_RANGE)


# ══════════════════════════════════════════════════════════════════════════
# STUDENT REGISTRATION (staff / admin / superadmin)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/register-form")
@login_required
def register_form():
    role          = session.get("role")
    user_college  = session.get("college")
    user_sid      = session.get("student_id", "")

    # Students can only register themselves — pre-fill their data
    if role == "student":
        if not user_sid:
            return render_template("auth_message.html",
                title="لا يوجد رقم طالب",
                msg="حسابك غير مرتبط برقم طالب. تواصل مع إدارة الكلية.",
                type="error")
        # Check if already registered
        db  = get_db(); cur = db.cursor()
        cur.execute(f"SELECT * FROM students WHERE student_id={ph()}", (user_sid,))
        existing = _row_to_dict(cur.fetchone()); db.close()
        if existing:
            return redirect(url_for("student_card", student_id=user_sid))

    visible_colleges = [user_college] if role == "admin" and user_college else COLLEGES
    # For student role, derive year from student_id prefix
    prefill_year = user_sid[:4] if role == "student" and len(user_sid) >= 4 else ""
    prefill_code = user_sid[4:] if role == "student" and len(user_sid) > 4  else ""

    return render_template("register.html",
                           colleges=visible_colleges,
                           years=YEAR_RANGE,
                           role=role,
                           user_name=session.get("user_name"),
                           locked_college=user_college if role in ("admin","student") else None,
                           locked_student_id=user_sid if role == "student" else None,
                           prefill_year=prefill_year,
                           prefill_code=prefill_code)


@app.route("/register", methods=["POST"])
@login_required
@limiter.limit(lambda: os.getenv("LIMIT_STAFF_REGISTER", "300 per hour"))
def register():
    try:
        full_name   = request.form.get("full_name","").strip()
        year        = to_eng(request.form.get("year","").strip())
        code        = to_eng(request.form.get("code","").strip())
        college     = request.form.get("college","").strip()
        student_email = request.form.get("student_email","").strip().lower()
        rotation    = int(request.form.get("rotation","0"))
        flip_h      = request.form.get("flip_h","") == "1"
        zoom        = float(request.form.get("zoom","1.0"))
        offset_x    = float(request.form.get("offset_x","0.0"))
        offset_y    = float(request.form.get("offset_y","0.0"))
        auto_crop   = request.form.get("auto_crop","1") == "1"
        image_file  = request.files.get("image")

        ok, msg = validate_full_name(full_name)
        if not ok: return jsonify(success=False, message=msg), 400
        ok, msg = validate_student_id(year, code)
        if not ok: return jsonify(success=False, message=msg), 400
        if not student_email:
            return jsonify(success=False, message="يرجى إدخال البريد الإلكتروني للطالب (إلزامي)"), 400
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", student_email):
            return jsonify(success=False, message="صيغة البريد الإلكتروني غير صحيحة"), 400
        if college not in COLLEGES:
            return jsonify(success=False, message="اختر كلية صحيحة"), 400

        # Admin can only register students in their own college
        if session.get("role") == "admin":
            allowed = session.get("college")
            if allowed and college != allowed:
                return jsonify(success=False,
                    message=f"يمكنك تسجيل طلاب كلية {allowed} فقط"), 403

        student_id = year + code
         # Student can ONLY register themselves
        if session.get("role") == "student":
          my_sid = session.get("student_id", "")
          if student_id != my_sid:
               return jsonify(success=False,
                 message="يمكنك تسجيل بياناتك الشخصية فقط"), 403

        # ── Check duplicate BEFORE processing image ──
        db  = get_db(); cur = db.cursor()
        cur.execute(f"SELECT student_id, full_name, image_path FROM students WHERE student_id={ph()}", (student_id,))
        existing = _row_to_dict(cur.fetchone()); db.close()
        if existing:
            img_url = None
            if existing.get("image_path"):
                p = existing["image_path"]
                img_url = p if p.startswith("http") else f"/static/{p}"
            return jsonify(
                success=False,
                duplicate=True,
                existing_name=existing["full_name"],
                existing_img=img_url,
                existing_id=existing["student_id"],
                message=f"الرقم {student_id} مسجل مسبقاً باسم: {existing['full_name']}"
            ), 409

        if not image_file or image_file.filename == "":
            return jsonify(success=False, message="يرجى رفع صورة شخصية"), 400
        if not image_file.filename.lower().endswith((".jpg",".jpeg")):
            return jsonify(success=False, message="يُسمح فقط بصور JPG"), 400

        raw_bytes = image_file.read()
        if len(raw_bytes) > app.config["MAX_CONTENT_LENGTH"]:
            return jsonify(success=False, message="حجم الصورة يتجاوز 5 MB"), 400

        # High-performance single-pass image processing and face validation
        ok, face_msg, processed = process_and_validate_photo(
            raw_bytes, rotation=rotation, flip_h=flip_h,
            zoom=zoom, offset_x=offset_x, offset_y=offset_y,
            auto_crop=auto_crop
        )
        if not ok:
            return jsonify(success=False, message=face_msg), 400

        # Save directly to local VPS storage
        result = save_image(processed, student_id, year, college, UPLOAD_FOLDER, skip_validation=True)

        # DB insert
        db  = get_db(); cur = db.cursor()
        try:
            uid = session.get("user_id")
            cur.execute(
                f"INSERT INTO students (student_id,full_name,year,college,email,image_path,registered_by) VALUES ({','.join([ph()]*7)})",
                (student_id, full_name, year, college,
                 student_email or None, result["path"], uid)
            )
            db.commit()

            # ── Create or update student user account so student can log in with password = student_id ──
            student_login_email = (student_email if (student_email and validate_university_email(student_email))
                                   else f"{student_id}@{UNIVERSITY_DOMAIN}")
            hashed_pw = hash_pw(student_id)
            cur.execute(f"SELECT id FROM users WHERE email={ph()} OR student_id={ph()}", (student_login_email, student_id))
            existing_user = cur.fetchone()
            if not existing_user:
                cur.execute(
                    f"INSERT INTO users (email,password_hash,password_plain,full_name,role,college,student_id,is_active,email_verified) VALUES ({','.join([ph()]*9)})",
                    (student_login_email, hashed_pw, student_id, full_name, "student",
                     college, student_id, True if is_use_pg() else 1, True if is_use_pg() else 1)
                )
                db.commit()
            else:
                user_id_val = existing_user["id"] if isinstance(existing_user, dict) else existing_user[0]
                cur.execute(
                    f"UPDATE users SET password_hash={ph()}, password_plain={ph()}, student_id={ph()}, full_name={ph()}, college={ph()}, is_active={ph()}, email_verified={ph()} WHERE id={ph()}",
                    (hashed_pw, student_id, student_id, full_name, college, True if is_use_pg() else 1, True if is_use_pg() else 1, user_id_val)
                )
                db.commit()

            log_action(uid, "REGISTER_STUDENT", target=student_id,
                       detail=full_name, ip=request.remote_addr)
        except Exception as e:
            db.close()
            # Clean up saved file
            if not result.get("cloudinary") and os.path.exists(
                    os.path.join(STATIC_ROOT, result["path"])):
                os.remove(os.path.join(STATIC_ROOT, result["path"]))
            if "UNIQUE" in str(e) or "unique" in str(e).lower():
                return jsonify(success=False,
                    message=f"⚠️ الرقم {student_id} مسجل مسبقاً", duplicate=True), 409
            raise
        db.close()

        # Send confirmation email if student provided email
        if student_email:
            card_link = url_for("student_card", student_id=student_id, _external=True)
            send_email(student_email, "تم تسجيلك في النظام الجامعي",
                       _email_registered_html(full_name, student_id, student_login_email, student_id, card_link))

        return jsonify(success=True,
            message=f"✅ تم تسجيل {full_name} بنجاح! الرقم: {student_id}",
            student_id=student_id,
            card_url=url_for("student_card", student_id=student_id),
            image_url=result["url"]), 201

    except Exception as e:
        app.logger.exception("Register error")
        return jsonify(success=False, message="حدث خطأ داخلي"), 500


# ══════════════════════════════════════════════════════════════════════════
# STUDENT CARD  (public)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/student/<student_id>")
def student_card(student_id):
    student_id = to_eng(student_id.strip())
    db  = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM students WHERE student_id={ph()}", (student_id,))
    row = _row_to_dict(cur.fetchone())
    if not row:
        # Check student_id_history table for renamed IDs
        try:
            cur.execute(f"SELECT new_student_id FROM student_id_history WHERE old_student_id={ph()}", (student_id,))
            hist = cur.fetchone()
            if hist:
                new_sid = hist["new_student_id"] if isinstance(hist, dict) else hist[0]
                if new_sid and new_sid != student_id:
                    db.close()
                    return redirect(url_for("student_card", student_id=new_sid), code=302)
        except Exception:
            pass
        # Fallback check if student user was updated
        try:
            cur.execute(f"SELECT student_id FROM users WHERE student_id={ph()} OR email LIKE {ph()}", (student_id, f"{student_id}@%"))
            u_match = cur.fetchone()
            if u_match:
                new_sid = u_match["student_id"] if isinstance(u_match, dict) else u_match[0]
                if new_sid and new_sid != student_id:
                    db.close()
                    return redirect(url_for("student_card", student_id=new_sid), code=302)
        except Exception:
            pass
        db.close()
        abort(404)
    db.close()
    # The student card can ONLY be edited by the student who owns it
    can_edit = (session.get("role") == "student" and session.get("student_id") == student_id)
    return render_template("student_card.html", student=row, can_edit=can_edit)


@app.route("/student/<student_id>/update-photo", methods=["POST"])
@limiter.limit(lambda: os.getenv("LIMIT_UPDATE_PHOTO", "30 per hour"))
def update_photo(student_id):
    student_id = to_eng(student_id.strip())
    
    # ── Authentication and Authorization checks ──
    uid = session.get("user_id")
    if not uid:
        return jsonify(success=False, message="غير مصرح بالدخول. يرجى تسجيل الدخول أولاً"), 401
        
    db  = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM students WHERE student_id={ph()}", (student_id,))
    row = _row_to_dict(cur.fetchone())
    if not row:
        db.close()
        return jsonify(success=False, message="غير موجود"), 404

    role = session.get("role")
    my_sid = session.get("student_id")
    
    # Editing photo from the student card URL is strictly restricted to the student who owns it
    if role != "student" or not my_sid or my_sid != student_id:
        db.close()
        return jsonify(success=False, message="تعديل الصورة من خلال هذه الصفحة متاح للطالب صاحب البطاقة فقط"), 403

    image_file = request.files.get("image")
    if not image_file or not image_file.filename.lower().endswith((".jpg",".jpeg")):
        db.close(); return jsonify(success=False, message="يرجى رفع صورة JPG"), 400

    rotation = int(request.form.get("rotation","0"))
    flip_h   = request.form.get("flip_h","") == "1"
    zoom     = float(request.form.get("zoom","1.0"))
    offset_x = float(request.form.get("offset_x","0.0"))
    offset_y = float(request.form.get("offset_y","0.0"))
    auto_crop = request.form.get("auto_crop","1") == "1"

    raw      = image_file.read()
    if len(raw) > app.config["MAX_CONTENT_LENGTH"]:
        db.close(); return jsonify(success=False, message="الصورة أكبر من 5 MB"), 400

    try:
        ok, face_msg, processed = process_and_validate_photo(
            raw, rotation=rotation, flip_h=flip_h,
            zoom=zoom, offset_x=offset_x, offset_y=offset_y,
            auto_crop=auto_crop
        )
        if not ok:
            db.close()
            return jsonify(success=False, message=face_msg), 400

        # Archive old
        old_rel = row.get("image_path","")
        archived_path = archive_old_image(old_rel, student_id, STATIC_ROOT, UPLOAD_FOLDER)
        if archived_path:
            app.logger.info(f"Archived old image to: {archived_path}")

        # Save new directly to local VPS storage
        result = save_image(processed, student_id, row["year"], row["college"], UPLOAD_FOLDER, skip_validation=True)

        cur.execute(
            f"UPDATE students SET image_path={ph()}, updated_at={ph()} WHERE student_id={ph()}",
            (result["path"], datetime.utcnow().isoformat(), student_id)
        )
        db.commit()
        uid = session.get("user_id")
        log_action(uid, "UPDATE_PHOTO", target=student_id,
                   detail="photo updated via student card", ip=request.remote_addr)

        db.close()
        return jsonify(
            success=True,
            message="تم تحديث الصورة بنجاح ✓",
            url=result["url"],
            new_url=result["url"],
            path=result["path"]
        )
    except Exception as e:
        db.close()
        app.logger.error(f"Error updating photo for {student_id}: {e}")
        return jsonify(success=False, message="حدث خطأ أثناء معالجة الصورة. تأكد من أن الملف المرفوع صورة صالحة وغير تالفة."), 500


@app.route("/api/crop-preview", methods=["POST"])
def api_crop_preview():
    """
    Instant preview endpoint: returns professionally cropped 400x500 face photo
    directly as a base64 data URL.
    """
    uid = session.get("user_id")
    if not uid:
        return jsonify(success=False, message="غير مصرح بالدخول"), 401

    image_file = request.files.get("image")
    if not image_file:
        return jsonify(success=False, message="لم يتم إرسال أي صورة"), 400

    raw_bytes = image_file.read()
    if len(raw_bytes) > app.config["MAX_CONTENT_LENGTH"]:
        return jsonify(success=False, message="حجم الصورة يتجاوز 5 MB"), 400

    ok, face_msg, processed = process_and_validate_photo(raw_bytes, auto_crop=True)
    if not ok:
        return jsonify(success=False, message=face_msg), 400

    b64 = base64.b64encode(processed).decode("utf-8")
    return jsonify(success=True, preview=f"data:image/jpeg;base64,{b64}")


# ══════════════════════════════════════════════════════════════════════════
# ADMIN – STUDENTS TABLE
# ══════════════════════════════════════════════════════════════════════════

@app.route("/admin/students")
@role_required("superadmin","admin","staff")
def admin_students():
    q        = request.args.get("q","").strip()
    year     = request.args.get("year","").strip()
    college  = request.args.get("college","").strip()
    page     = max(int(request.args.get("page",1)),1)
    per_page = 20
    role     = session.get("role")
    user_college = session.get("college")  # admin restricted to own college

    conditions, params = [], []
    if q:
        if is_use_pg():
            conditions.append("(student_id ILIKE %s OR full_name ILIKE %s)")
        else:
            conditions.append("(student_id LIKE ? OR full_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if year:    conditions.append(f"year={ph()}");    params.append(year)
    if college: conditions.append(f"college={ph()}"); params.append(college)
    # admin can only see own college
    if role == "admin" and user_college:
        conditions.append(f"college={ph()}"); params.append(user_college)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    db    = get_db(); cur = db.cursor()
    cur.execute(f"SELECT COUNT(*) AS c FROM students {where}", params)
    total  = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)
    offset = (page-1)*per_page
    if is_use_pg():
        cur.execute(f"SELECT * FROM students {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    params+[per_page, offset])
    else:
        cur.execute(f"SELECT * FROM students {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    params+[per_page, offset])
    rows  = [_row_to_dict(r) for r in cur.fetchall()]
    db.close()
    return jsonify(students=rows, total=total, page=page,
                   pages=(total+per_page-1)//per_page)


@app.route("/admin/delete/<int:sid>", methods=["DELETE"])
@role_required("superadmin")
def admin_delete(sid):
    db  = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM students WHERE id={ph()}", (sid,))
    row = _row_to_dict(cur.fetchone())
    if not row: db.close(); return jsonify(success=False, message="غير موجود"), 404
    img_path = os.path.join(STATIC_ROOT, row.get("image_path",""))
    cur.execute(f"DELETE FROM students WHERE id={ph()}", (sid,))
    db.commit(); db.close()
    if os.path.exists(img_path): os.remove(img_path)
    log_action(session.get("user_id"), "DELETE_STUDENT",
               target=row.get("student_id"), detail=row.get("full_name"),
               ip=request.remote_addr)
    return jsonify(success=True, message="تم حذف الطالب بنجاح")


@app.route("/admin/student/<int:sid>/edit", methods=["POST"])
@role_required("superadmin", "admin", "staff")
def admin_edit_student(sid):
    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM students WHERE id={ph()}", (sid,))
    s = _row_to_dict(cur.fetchone())
    if not s:
        db.close()
        return jsonify(success=False, message="الطالب غير موجود"), 404

    current_role = session.get("role")
    user_college = session.get("college")

    # If supervisor (admin), can only edit students of their own college
    if current_role == "admin" and s.get("college") != user_college:
        db.close()
        return jsonify(success=False, message="ليس لديك صلاحية لتعديل بيانات طالب من كلية أخرى"), 403

    old_student_id = s.get("student_id")
    old_college = s.get("college")
    old_year = s.get("year")
    old_name = s.get("full_name")
    old_email = s.get("email")

    image_file = request.files.get("image")

    # College Supervisor (admin) can ONLY update the student's photo, NOT any other data!
    if current_role == "admin":
        full_name = old_name
        email = old_email
        college = old_college
        year = old_year
        new_student_id = old_student_id
        if not (image_file and image_file.filename):
            db.close()
            return jsonify(success=False, message="مشرف الكلية مصرح له بتعديل الصورة الشخصية فقط. يرجى اختيار صورة جديدة."), 400
    else:
        # superadmin and staff can update all fields
        full_name = request.form.get("full_name", old_name).strip()
        email = request.form.get("email", "").strip().lower()
        college = request.form.get("college", old_college).strip()
        year = to_eng(request.form.get("year", old_year).strip())
        new_student_id = to_eng(request.form.get("student_id", old_student_id).strip())

        if not full_name:
            db.close()
            return jsonify(success=False, message="يرجى إدخال اسم الطالب الكامل"), 400

        # Email is mandatory
        if not email:
            db.close()
            return jsonify(success=False, message="يرجى إدخال البريد الإلكتروني للطالب (إلزامي)"), 400
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            db.close()
            return jsonify(success=False, message="صيغة البريد الإلكتروني غير صحيحة"), 400

        if college not in COLLEGES:
            db.close()
            return jsonify(success=False, message="الكلية المحددة غير صالحة"), 400

        # If student_id changed, check uniqueness
        if new_student_id != old_student_id:
            cur.execute(f"SELECT id FROM students WHERE student_id={ph()} AND id!={ph()}", (new_student_id, sid))
            if cur.fetchone():
                db.close()
                return jsonify(success=False, message=f"الرقم الجامعي {new_student_id} مسجل لطالب آخر بالفعل"), 409

    changed_location = (college != old_college or year != old_year or new_student_id != old_student_id)
    new_image_path = s.get("image_path")
    if image_file and image_file.filename:
        raw_img = image_file.read()
        if len(raw_img) > app.config["MAX_CONTENT_LENGTH"]:
            db.close()
            return jsonify(success=False, message="حجم الصورة يتجاوز 5 MB"), 400
        try:
            ok, face_msg, processed = process_and_validate_photo(raw_img, auto_crop=True)
            if not ok:
                db.close()
                return jsonify(success=False, message=face_msg), 400
            archived = archive_old_image(s.get("image_path"), old_student_id, STATIC_ROOT, UPLOAD_FOLDER)
            res_img = save_image(processed, new_student_id, year, college, UPLOAD_FOLDER)
            new_image_path = res_img["path"]
            if changed_location:
                # Remove active file in old college path if it differed
                old_full = os.path.join(STATIC_ROOT, s.get("image_path", "")) if s.get("image_path") else ""
                new_full = os.path.join(STATIC_ROOT, new_image_path)
                if old_full and os.path.exists(old_full) and os.path.abspath(old_full) != os.path.abspath(new_full):
                    try:
                        os.remove(old_full)
                    except Exception:
                        pass
                move_student_in_gdrive(old_student_id, old_year, old_college, new_student_id, year, college)
        except Exception as e:
            db.close()
            return jsonify(success=False, message=f"فشل في معالجة الصورة: {e}"), 400
    elif changed_location:
        # Move existing active and archived images locally and on Google Drive
        try:
            moved_path = move_student_images_locally(
                old_student_id=old_student_id,
                old_year=old_year,
                old_college=old_college,
                new_student_id=new_student_id,
                new_year=year,
                new_college=college,
                old_rel_path=s.get("image_path"),
                static_root=STATIC_ROOT,
                upload_root=UPLOAD_FOLDER
            )
            if moved_path:
                new_image_path = moved_path
        except Exception as e:
            print(f"Error moving student images locally / gdrive: {e}")

    if not new_image_path:
        new_image_path = s.get("image_path")

    # 1. Update students table
    cur.execute(
        f"""UPDATE students 
            SET student_id={ph()}, full_name={ph()}, year={ph()}, college={ph()}, 
                email={ph()}, image_path={ph()}, updated_at={ph()} 
            WHERE id={ph()}""",
        (new_student_id, full_name, year, college, email or None, new_image_path, datetime.utcnow().isoformat(), sid)
    )

    # 1b. Record in student_id_history for automatic redirection from old URLs
    if new_student_id != old_student_id:
        try:
            if is_use_pg():
                cur.execute(
                    "INSERT INTO student_id_history (old_student_id, new_student_id) VALUES (%s, %s) ON CONFLICT (old_student_id) DO UPDATE SET new_student_id=EXCLUDED.new_student_id",
                    (old_student_id, new_student_id)
                )
            else:
                cur.execute(
                    "INSERT INTO student_id_history (old_student_id, new_student_id) VALUES (?, ?) ON CONFLICT (old_student_id) DO UPDATE SET new_student_id=excluded.new_student_id",
                    (old_student_id, new_student_id)
                )
            cur.execute(f"UPDATE student_id_history SET new_student_id={ph()} WHERE new_student_id={ph()}", (new_student_id, old_student_id))
        except Exception as e_hist:
            app.logger.warning(f"Error recording student_id_history: {e_hist}")

    # 2. Comprehensive cascade update to users table
    # Ensures student login, user management list, and student credentials reflect changes everywhere
    student_login_email = (email if (email and validate_university_email(email))
                           else f"{new_student_id}@{UNIVERSITY_DOMAIN}")

    cur.execute(
        f"SELECT id, email, password_plain FROM users WHERE role='student' AND (student_id={ph()} OR email={ph()} OR student_id={ph()})",
        (old_student_id, old_email or "", new_student_id)
    )
    user_matches = cur.fetchall()
    if user_matches:
        for u_match in user_matches:
            user_id_val = u_match["id"] if isinstance(u_match, dict) else u_match[0]
            curr_plain = u_match["password_plain"] if isinstance(u_match, dict) else (u_match[2] if len(u_match) > 2 else None)

            if curr_plain == old_student_id or not curr_plain:
                # Password was still default student_id, keep it synced to new ID
                new_hashed_pw = hash_pw(new_student_id)
                cur.execute(
                    f"""UPDATE users 
                        SET student_id={ph()}, full_name={ph()}, college={ph()}, 
                            email={ph()}, password_plain={ph()}, password_hash={ph()} 
                        WHERE id={ph()}""",
                    (new_student_id, full_name, college, student_login_email, new_student_id, new_hashed_pw, user_id_val)
                )
            else:
                # Custom password preserved, update user identity and college
                cur.execute(
                    f"""UPDATE users 
                        SET student_id={ph()}, full_name={ph()}, college={ph()}, email={ph()} 
                        WHERE id={ph()}""",
                    (new_student_id, full_name, college, student_login_email, user_id_val)
                )
    else:
        # Create student user account if it did not exist
        new_hashed_pw = hash_pw(new_student_id)
        cur.execute(
            f"INSERT INTO users (email,password_hash,password_plain,full_name,role,college,student_id,is_active,email_verified) VALUES ({','.join([ph()]*9)})",
            (student_login_email, new_hashed_pw, new_student_id, full_name, "student",
             college, new_student_id, True if is_use_pg() else 1, True if is_use_pg() else 1)
        )

    db.commit()
    db.close()

    log_action(session.get("user_id"), "EDIT_STUDENT", target=new_student_id,
               detail=f"Updated by {session.get('user_name')} (Old ID: {old_student_id}, College: {old_college} -> {college})",
               ip=request.remote_addr)

    return jsonify(success=True, message="تم حفظ وتحديث بيانات الطالب بنجاح ✓")


@app.route("/admin/export")
@role_required("superadmin","admin","staff")
def admin_export():
    db  = get_db(); cur = db.cursor()
    where, params = "", []
    if session.get("role") == "admin" and session.get("college"):
        where  = f"WHERE college={ph()}"
        params = [session.get("college")]
    cur.execute(f"SELECT student_id,full_name,year,college,email,created_at FROM students {where} ORDER BY created_at DESC", params)
    rows = [_row_to_dict(r) for r in cur.fetchall()]; db.close()

    wb = Workbook(); ws = wb.active; ws.title = "الطلاب"
    ws.sheet_view.rightToLeft = True
    hf = Font(name="Arial", bold=True, color="FFFFFF", size=12)
    hb = PatternFill("solid", fgColor="0D1F3C")
    ca = Alignment(horizontal="center", vertical="center", wrap_text=True)
    th = Side(style="thin", color="CCCCCC")
    bd = Border(left=th,right=th,top=th,bottom=th)
    af = PatternFill("solid", fgColor="EBF2FA")
    headers = ["رقم الطالب","الاسم الكامل","السنة","الكلية","الإيمبيل","تاريخ التسجيل"]
    widths  = [18,42,10,36,32,22]
    for ci,(h,w) in enumerate(zip(headers,widths),1):
        cell = ws.cell(1,ci,h); cell.font=hf; cell.fill=hb
        cell.alignment=ca; cell.border=bd
        ws.column_dimensions[cell.column_letter].width=w
    ws.row_dimensions[1].height=28
    for ri,r in enumerate(rows,2):
        for ci,v in enumerate([r.get("student_id"),r.get("full_name"),
                                r.get("year"),r.get("college"),
                                r.get("email",""),r.get("created_at")],1):
            cell=ws.cell(ri,ci,str(v) if v else "")
            cell.alignment=ca; cell.border=bd
            if ri%2==0: cell.fill=af
        ws.row_dimensions[ri].height=22
    ws.freeze_panes="A2"
    ws.auto_filter.ref=f"A1:F{len(rows)+1}"
    tmp=tempfile.NamedTemporaryFile(suffix=".xlsx",delete=False)
    wb.save(tmp.name); tmp.close()
    now=datetime.now().strftime("%Y%m%d_%H%M%S")
    log_action(session.get("user_id"),"EXPORT_EXCEL",ip=request.remote_addr)
    return send_file(tmp.name,as_attachment=True,
                     download_name=f"students_{now}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/export-photos")
@role_required("superadmin", "staff")
def admin_export_photos():
    """
    Download all or filtered student photos as a ZIP archive directly from VPS storage.
    Staff and Superadmin can export all or filter by college/year.
    College Admin (supervisors) are not permitted to download photos.
    """
    college_filter = request.args.get("college", "").strip()
    year_filter = request.args.get("year", "").strip()

    where_clauses = ["image_path IS NOT NULL", "image_path != ''"]
    params = []

    if college_filter and college_filter != "all":
        where_clauses.append(f"college = {ph()}")
        params.append(college_filter)

    if year_filter and year_filter != "all":
        where_clauses.append(f"year = {ph()}")
        params.append(year_filter)

    where_sql = " AND ".join(where_clauses)
    query = f"SELECT student_id, full_name, year, college, image_path FROM students WHERE {where_sql} ORDER BY college, year, student_id"

    db = get_db()
    cur = db.cursor()
    cur.execute(query, params)
    students = [_row_to_dict(r) for r in cur.fetchall()]
    db.close()

    if not students:
        return jsonify(success=False, message="لا توجد صور مطابقة لخيارات التصفية المحددة"), 404

    # Create temporary zip archive on VPS disk to avoid memory spike
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_zip_path = tmp_zip.name
    tmp_zip.close()

    added_count = 0
    with zipfile.ZipFile(tmp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest_lines = ["رقم الطالب,الاسم الكامل,الكلية,سنة القيد,اسم الملف"]
        for s in students:
            rel = s.get("image_path", "")
            if not rel:
                continue

            # Local VPS file path resolution
            local_path = os.path.join(STATIC_ROOT, rel.replace("/", os.sep))
            if not os.path.exists(local_path):
                # Check directly inside UPLOAD_FOLDER
                local_path = os.path.join(UPLOAD_FOLDER, rel.replace("uploads/", "").replace("uploads\\", "").replace("/", os.sep))

            if os.path.exists(local_path) and os.path.isfile(local_path):
                c_name = re.sub(r'[\s/\\:*?"<>|]+', "_", s["college"].strip())
                y_name = s.get("year", "عام")
                s_id = s.get("student_id", "")

                # Clean naming: College / Year / ID.jpg (Strictly ID only, no name)
                archive_arcname = f"{c_name}/{y_name}/{s_id}.jpg"
                zf.write(local_path, arcname=archive_arcname)
                manifest_lines.append(f'"{s_id}","{s.get("full_name","")}","{s["college"]}","{y_name}","{archive_arcname}"')
                added_count += 1

        # Add manifest file
        zf.writestr("manifest.csv", "\ufeff" + "\n".join(manifest_lines))

    if added_count == 0:
        if os.path.exists(tmp_zip_path):
            try: os.remove(tmp_zip_path)
            except Exception: pass
        return jsonify(success=False, message="لم يتم العثور على ملفات صور على السيرفر للطلاب المحددين"), 404

    log_action(
        session.get("user_id"),
        "EXPORT_PHOTOS_ZIP",
        detail=f"Exported {added_count} photos (College: {college_filter or 'All'}, Year: {year_filter or 'All'})",
        ip=request.remote_addr
    )

    safe_col = "all" if not college_filter or college_filter == "all" else re.sub(r'[\s/\\:*?"<>|]+', "_", college_filter)
    download_filename = f"bua_photos_{safe_col}_{now_str}.zip"

    return send_file(
        tmp_zip_path,
        as_attachment=True,
        download_name=download_filename,
        mimetype="application/zip"
    )


# ══════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT  (superadmin only)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/admin/users")
@role_required("superadmin")
def admin_users():
    db  = get_db(); cur = db.cursor()
    cur.execute("""SELECT id,email,full_name,role,college,student_id,
                          is_active,email_verified,created_at,password_plain
                   FROM users ORDER BY college NULLS LAST, role, full_name""")
    users = [_row_to_dict(r) for r in cur.fetchall()]
    db.close()

    # Group by college for display
    grouped = {}
    for u in users:
        key = u.get("college") or "إدارة النظام"
        grouped.setdefault(key, []).append(u)

    return jsonify(users=users, grouped={k: v for k, v in sorted(grouped.items())})


@app.route("/admin/users/create", methods=["POST"])
@role_required("superadmin")
def admin_create_user():
    data      = request.get_json() or {}
    email     = (data.get("email") or "").strip().lower()
    role      = data.get("role", "staff")
    college   = data.get("college") or None
    full_name = (data.get("full_name") or "").strip()
    password  = (data.get("password") or "").strip()

    if not full_name or len(full_name.split()) < 2:
        return jsonify(success=False, message="يرجى إدخال الاسم الكامل للمستخدم"), 400
    if not re.fullmatch(r"^[\u0621-\u064A\u0671\s]+$", full_name):
        return jsonify(success=False, message="الاسم يجب أن يكون باللغة العربية فقط"), 400
    if not validate_university_email(email):
        return jsonify(success=False, message=f"يجب استخدام بريد الجامعة (@{UNIVERSITY_DOMAIN})"), 400
    if role not in ROLES or role == "student":
        return jsonify(success=False, message="دور غير صالح — الطلاب يسجلون بأنفسهم"), 400
    if not password or len(password) < 8:
        return jsonify(success=False, message="كلمة المرور يجب أن تكون 8 أحرف على الأقل"), 400

    hashed = hash_pw(password)
    db = get_db(); cur = db.cursor()
    try:
        cur.execute(
            f"INSERT INTO users (email,password_hash,password_plain,full_name,role,college,email_verified,is_active) VALUES ({','.join([ph()]*8)})",
            (email, hashed, password, full_name, role, college, 1 if not is_use_pg() else True, 1 if not is_use_pg() else True)
        )
        db.commit()
    except Exception as e:
        db.close()
        return jsonify(success=False, message="البريد الإلكتروني مسجل مسبقاً"), 409
    db.close()

    try:
        send_email(
            email,
            "تم إنشاء حسابك في نظام التسجيل الجامعي",
            f"""
            <div dir="rtl" style="font-family:Cairo,Arial;max-width:520px;margin:auto">
              <div style="background:#0d1f3c;padding:28px;border-radius:14px 14px 0 0;text-align:center">
                <h2 style="color:#e8b84b;margin:0">مرحباً بك في نظام التسجيل</h2>
              </div>
              <div style="background:#f0f4f9;padding:28px;border-radius:0 0 14px 14px">
                <p>أهلاً <strong>{full_name}</strong>،</p>
                <p>تم إنشاء حساب لك بدور: <strong>{ROLES.get(role, role)}</strong>.</p>
                <p>يمكنك الآن تسجيل الدخول باستخدام بريدك الجامعي وكلمة المرور المحددة لك من قبل الإدارة: <strong>{password}</strong></p>
                <p style="color:#6b7a99;font-size:.85rem">يمكنك تغيير كلمة مرورك في أي وقت بسهولة من داخل حسابك بعد تسجيل الدخول.</p>
              </div>
            </div>
            """
        )
    except Exception:
        pass

    log_action(session.get("user_id"), "CREATE_USER", target=email, detail=f"Role: {role}", ip=request.remote_addr)
    return jsonify(success=True, message=f"تم إنشاء حساب {full_name} بنجاح ويمكنه تسجيل الدخول بكلمة المرور المحددة")


@app.route("/admin/users/<int:uid>/reset-password", methods=["POST"])
@role_required("superadmin")
def admin_reset_user_password(uid):
    """Allow superadmin to set/reset any user's password directly."""
    data = request.get_json() if request.is_json else request.form
    new_pw = (data.get("new_password") or "").strip()
    if not new_pw or len(new_pw) < 8:
        return jsonify(success=False, message="كلمة المرور الجديدة يجب أن تكون 8 أحرف على الأقل"), 400

    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT email, full_name FROM users WHERE id={ph()}", (uid,))
    u = _row_to_dict(cur.fetchone())
    if not u:
        db.close()
        return jsonify(success=False, message="المستخدم غير موجود"), 404

    new_hashed = hash_pw(new_pw)
    cur.execute(f"UPDATE users SET password_hash={ph()}, password_plain={ph()} WHERE id={ph()}", (new_hashed, new_pw, uid))
    db.commit()
    db.close()

    log_action(session.get("user_id"), "ADMIN_RESET_PASSWORD", target=u["email"], detail=f"Reset password for {u['full_name']}", ip=request.remote_addr)
    return jsonify(success=True, message=f"تم تعيين كلمة المرور الجديدة للمستخدم {u['full_name']} بنجاح ✓")


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@role_required("superadmin")
def admin_toggle_user(uid):
    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT is_active,email FROM users WHERE id={ph()}", (uid,))
    u = _row_to_dict(cur.fetchone())
    if not u: db.close(); return jsonify(success=False), 404
    new_val = (not u["is_active"]) if is_use_pg() else (0 if u["is_active"] else 1)
    cur.execute(f"UPDATE users SET is_active={ph()} WHERE id={ph()}", (new_val, uid))
    db.commit(); db.close()
    log_action(session.get("user_id"), "TOGGLE_USER", target=u["email"], ip=request.remote_addr)
    return jsonify(success=True, active=bool(new_val))


@app.route("/admin/users/<int:uid>/edit", methods=["POST"])
@role_required("superadmin")
def admin_edit_user(uid):
    data = request.get_json() if request.is_json else request.form
    full_name = (data.get("full_name") or "").strip()
    role = data.get("role")
    college = data.get("college") or None

    if not full_name:
        return jsonify(success=False, message="يرجى إدخال اسم المستخدم"), 400
    if role not in ROLES or role == "student":
        return jsonify(success=False, message="الدور المحدد غير صالح"), 400

    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT email, role, full_name FROM users WHERE id={ph()}", (uid,))
    u = _row_to_dict(cur.fetchone())
    if not u:
        db.close()
        return jsonify(success=False, message="المستخدم غير موجود"), 404

    cur.execute(
        f"UPDATE users SET full_name={ph()}, role={ph()}, college={ph()} WHERE id={ph()}",
        (full_name, role, college, uid)
    )
    db.commit()
    db.close()

    log_action(session.get("user_id"), "EDIT_USER_ROLE", target=u["email"],
               detail=f"Updated role to {role}, College to {college}", ip=request.remote_addr)
    return jsonify(success=True, message=f"تم تحديث صلاحيات المشرف {full_name} بنجاح ✓")


@app.route("/admin/users/<int:uid>/delete", methods=["DELETE"])
@role_required("superadmin")
def admin_delete_user(uid):
    if uid == session.get("user_id"):
        return jsonify(success=False, message="لا يمكنك حذف حسابك الشخصي"), 400

    db = get_db(); cur = db.cursor()
    cur.execute(f"SELECT email, role, full_name FROM users WHERE id={ph()}", (uid,))
    u = _row_to_dict(cur.fetchone())
    if not u:
        db.close()
        return jsonify(success=False, message="المستخدم غير موجود"), 404
    if u["role"] == "superadmin":
        db.close()
        return jsonify(success=False, message="لا يمكن حذف حساب مدير النظام الرئيسي"), 403

    cur.execute(f"UPDATE audit_log SET user_id=NULL WHERE user_id={ph()}", (uid,))
    cur.execute(f"UPDATE students SET registered_by=NULL WHERE registered_by={ph()}", (uid,))
    cur.execute(f"DELETE FROM users WHERE id={ph()}", (uid,))
    db.commit()
    db.close()

    log_action(session.get("user_id"), "DELETE_USER", target=u["email"],
               detail=f"Deleted user {u['full_name']}", ip=request.remote_addr)
    return jsonify(success=True, message=f"تم حذف حساب المستخدم {u['full_name']} بنجاح")


def export_users_to_excel_bytes() -> bytes:
    """Generate an Excel workbook with all users and plain-text passwords."""
    db = get_db(); cur = db.cursor()
    cur.execute("""SELECT id, email, full_name, role, college, student_id,
                          password_plain, is_active, email_verified, created_at
                   FROM users ORDER BY id ASC""")
    users = [_row_to_dict(r) for r in cur.fetchall()]
    db.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "المستخدمون وكلمات المرور"
    ws.views.sheetView[0].rightToLeft = True

    # Styles
    navy_fill  = PatternFill(start_color="0D1F3C", end_color="0D1F3C", fill_type="solid")
    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    dark_font  = Font(name="Arial", size=10, bold=False, color="1A2744")
    mono_font  = Font(name="Courier New", size=10, bold=True, color="0D1F3C")

    thin_border = Border(
        left=Side(style='thin', color='DCE3EF'),
        right=Side(style='thin', color='DCE3EF'),
        top=Side(style='thin', color='DCE3EF'),
        bottom=Side(style='thin', color='DCE3EF')
    )

    headers = [
        "م", "الاسم الكامل", "البريد الإلكتروني", "كلمة المرور",
        "الرقم الجامعي", "الكلية", "نوع الحساب", "حالة الحساب",
        "تأكيد البريد", "تاريخ التسجيل"
    ]

    ws.append(headers)
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = navy_fill
        cell.font = white_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    ws.row_dimensions[1].height = 28

    role_map = {
        "superadmin": "مدير رئيسي",
        "admin": "مشرف كلية",
        "staff": "موظف شئون طلاب",
        "student": "طالب"
    }

    for idx, u in enumerate(users, start=2):
        role_arabic   = role_map.get(u.get("role"), u.get("role") or "")
        active_text   = "نشط" if u.get("is_active") else "موقوف"
        verified_text = "مفعّل" if u.get("email_verified") else "غير مفعّل"
        created_str   = str(u.get("created_at") or "")[:19]

        row_vals = [
            u.get("id"),
            u.get("full_name") or "",
            u.get("email") or "",
            u.get("password_plain") or "",
            u.get("student_id") or "",
            u.get("college") or "إدارة عامة",
            role_arabic,
            active_text,
            verified_text,
            created_str
        ]
        ws.append(row_vals)

        is_even = (idx % 2 == 0)
        for c_idx in range(1, len(row_vals) + 1):
            c = ws.cell(row=idx, column=c_idx)
            c.border = thin_border
            c.font = dark_font
            if is_even:
                c.fill = zebra_fill
            if c_idx == 4:  # Password
                c.font = mono_font
                c.alignment = Alignment(horizontal="center", vertical="center")
            elif c_idx in (1, 5, 7, 8, 9, 10):
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.alignment = Alignment(horizontal="right", vertical="center")
        ws.row_dimensions[idx].height = 22

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def backup_users_to_gdrive() -> tuple[bool, str]:
    """Export all users to an Excel spreadsheet and upload it to Google Drive backups folder."""
    try:
        data = export_users_to_excel_bytes()
        ok = upload_backup_to_gdrive(data, "users_backup.xlsx")
        if ok:
            return True, "تم رفع النسخة الاحتياطية للمستخدمين إلى Google Drive بنجاح ✓"
        else:
            return False, "فشل الرفع إلى Google Drive (تأكد من تفعيل إعدادات GOOGLE_DRIVE في .env)"
    except Exception as e:
        app.logger.error(f"Google Drive users backup error: {e}")
        return False, f"خطأ أثناء النسخ الاحتياطي: {str(e)}"


@app.route("/admin/users/export-excel")
@role_required("superadmin")
def admin_export_users_excel():
    """Download users list with plain passwords as Excel file."""
    try:
        excel_bytes = export_users_to_excel_bytes()
        filename = f"users_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            io.BytesIO(excel_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        app.logger.error(f"Error exporting users Excel: {e}")
        return jsonify(success=False, message="حدث خطأ أثناء تصدير ملف الإكسيل"), 500


@app.route("/admin/users/backup-gdrive", methods=["POST"])
@role_required("superadmin")
def admin_backup_users_gdrive():
    """Trigger manual backup of users Excel to Google Drive."""
    ok, msg = backup_users_to_gdrive()
    if ok:
        log_action(session.get("user_id"), "BACKUP_USERS_GDRIVE", detail=msg, ip=request.remote_addr)
        return jsonify(success=True, message=msg)
    else:
        return jsonify(success=False, message=msg), 500


# ══════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ══════════════════════════════════════════════════════════════════════════

@app.route("/admin/audit")
@role_required("superadmin", "admin", "staff")
def admin_audit():
    page     = max(int(request.args.get("page", 1)), 1)
    per_page = 50
    offset   = (page - 1) * per_page
    db = get_db(); cur = db.cursor()
    role = session.get("role")
    user_college = session.get("college")

    where_sql = ""
    params = []

    if role == "admin" and user_college:
        # College supervisor sees strictly their own college's students' activities
        where_sql = f"""WHERE (
            (u.role = 'student' AND (u.college = {ph()} OR s.college = {ph()}))
            OR (a.target IN (SELECT student_id FROM students WHERE college = {ph()}))
            OR (a.target IN (SELECT student_id FROM users WHERE college = {ph()} AND role = 'student'))
            OR (a.target IN (SELECT email FROM users WHERE college = {ph()} AND role = 'student'))
            OR (a.target IN (SELECT h.old_student_id FROM student_id_history h JOIN students st ON h.new_student_id = st.student_id WHERE st.college = {ph()}))
        )"""
        params = [user_college, user_college, user_college, user_college, user_college, user_college]
    elif role == "staff":
        where_sql = """WHERE (
            a.action IN ('REGISTER_STUDENT', 'UPDATE_PHOTO', 'DELETE_STUDENT', 'EDIT_STUDENT', 'BULK_IMPORT_STUDENT', 'STUDENT_SELF_REGISTER')
            OR u.role = 'student'
        )"""

    count_query = f"""
        SELECT COUNT(*) AS c
        FROM audit_log a
        LEFT JOIN users u ON a.user_id = u.id
        LEFT JOIN students s ON (a.target = s.student_id OR (u.student_id IS NOT NULL AND u.student_id = s.student_id))
        {where_sql}
    """
    cur.execute(count_query, params)
    total = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)

    if is_use_pg():
        data_query = f"""
            SELECT a.*,
                   COALESCE(s.full_name, u.full_name, 'طالب') AS full_name,
                   COALESCE(s.email, u.email) AS email,
                   COALESCE(s.student_id, u.student_id, a.target) AS student_id,
                   COALESCE(s.college, u.college) AS student_college
            FROM audit_log a
            LEFT JOIN users u ON a.user_id = u.id
            LEFT JOIN students s ON (a.target = s.student_id OR (u.student_id IS NOT NULL AND u.student_id = s.student_id))
            {where_sql}
            ORDER BY a.created_at DESC LIMIT %s OFFSET %s
        """
        cur.execute(data_query, params + [per_page, offset])
    else:
        data_query = f"""
            SELECT a.*,
                   COALESCE(s.full_name, u.full_name, 'طالب') AS full_name,
                   COALESCE(s.email, u.email) AS email,
                   COALESCE(s.student_id, u.student_id, a.target) AS student_id,
                   COALESCE(s.college, u.college) AS student_college
            FROM audit_log a
            LEFT JOIN users u ON a.user_id = u.id
            LEFT JOIN students s ON (a.target = s.student_id OR (u.student_id IS NOT NULL AND u.student_id = s.student_id))
            {where_sql}
            ORDER BY a.created_at DESC LIMIT ? OFFSET ?
        """
        cur.execute(data_query, params + [per_page, offset])

    rows = [_row_to_dict(r) for r in cur.fetchall()]
    db.close()
    return jsonify(logs=rows, total=total, page=page, pages=(total + per_page - 1) // per_page)


# ══════════════════════════════════════════════════════════════════════════
# JWT API  (for external integrations)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/login", methods=["POST"])
@csrf.exempt
@limiter.limit(lambda: os.getenv("LIMIT_API_LOGIN", "60 per hour"))
def api_login():
    data  = request.get_json() or {}
    email = data.get("email","").strip().lower()
    pw    = data.get("password","")
    db    = get_db(); cur = db.cursor()
    cur.execute(f"SELECT * FROM users WHERE email={ph()}", (email,))
    u = _row_to_dict(cur.fetchone()); db.close()
    if not u or not u.get("is_active") or not u.get("email_verified"):
        return jsonify(msg="بيانات الدخول غير صحيحة"), 401
    if not check_pw(pw, u["password_hash"]):
        return jsonify(msg="بيانات الدخول غير صحيحة"), 401
    token = create_access_token(identity={"id":u["id"],"role":u["role"],"email":u["email"]})
    log_action(u["id"],"API_LOGIN",ip=request.remote_addr)
    return jsonify(access_token=token, role=u["role"])


@app.route("/api/students")
@jwt_required()
def api_students():
    page     = max(int(request.args.get("page",1)),1)
    per_page = 20; offset=(page-1)*per_page
    db = get_db(); cur = db.cursor()
    if is_use_pg():
        cur.execute("SELECT student_id,full_name,year,college,image_path,created_at FROM students ORDER BY created_at DESC LIMIT %s OFFSET %s", (per_page,offset))
    else:
        cur.execute("SELECT student_id,full_name,year,college,image_path,created_at FROM students ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page,offset))
    rows=[_row_to_dict(r) for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) AS c FROM students")
    total = int((_row_to_dict(cur.fetchone()) or {}).get("c", 0) or 0)
    db.close()
    return jsonify(students=rows, total=total, page=page)


@app.route("/api/students/<student_id>")
@jwt_required()
def api_student(student_id):
    db=get_db(); cur=db.cursor()
    cur.execute(f"SELECT * FROM students WHERE student_id={ph()}",(to_eng(student_id),))
    row=_row_to_dict(cur.fetchone()); db.close()
    if not row: return jsonify(msg="غير موجود"),404
    row.pop("id",None)
    return jsonify(row)


# ══════════════════════════════════════════════════════════════════════════
# MAIN ADMIN PAGE  (renders the SPA shell)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/admin")
@role_required("superadmin","admin","staff")
def admin_panel():
    db=get_db(); cur=db.cursor()
    cur.execute("SELECT DISTINCT year FROM students ORDER BY year DESC")
    yrs=[_row_to_dict(r)["year"] for r in cur.fetchall()]
    db.close()
    return render_template("admin_panel.html",
        years=yrs, all_years=YEAR_RANGE,
        colleges=COLLEGES, roles=ROLES,
        role=session.get("role"),
        user_name=session.get("user_name"),
        user_college=session.get("college"),
        domain=UNIVERSITY_DOMAIN)


# ══════════════════════════════════════════════════════════════════════════
# BULK IMPORT  –  superadmin uploads Excel, system creates student accounts
# ══════════════════════════════════════════════════════════════════════════

@app.route("/admin/bulk-import", methods=["GET", "POST"])
@role_required("superadmin")
def bulk_import():
    if request.method == "GET":
        return render_template("bulk_import.html",
            role=session.get("role"),
            user_name=session.get("user_name"),
            domain=UNIVERSITY_DOMAIN)

    # POST — process uploaded Excel/CSV
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(success=False, message="يرجى رفع ملف Excel أو CSV"), 400

    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("xlsx", "xls", "csv"):
        return jsonify(success=False, message="يُقبل xlsx أو csv فقط"), 400

    raw = f.read()

    rows = []
    try:
        if ext == "csv":
            import csv
            text   = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            rows   = list(reader)
        else:
            from openpyxl import load_workbook
            wb     = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            ws     = wb.active
            headers = [str(c.value or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
            for xl_row in ws.iter_rows(min_row=2, values_only=True):
                rows.append(dict(zip(headers, [str(v or "").strip() for v in xl_row])))
    except Exception as e:
        return jsonify(success=False, message=f"خطأ في قراءة الملف: {e}"), 400

    # Expected columns (flexible mapping)
    COL_MAP = {
        "student_id": ["student_id","رقم_الطالب","رقم الطالب","id","الرقم"],
        "full_name":  ["full_name","الاسم_الكامل","الاسم الكامل","name","الاسم"],
        "year":       ["year","السنة","العام","سنة"],
        "college":    ["college","الكلية","كلية"],
        "email":      ["email","الايميل","البريد","البريد_الإلكتروني","ايميل"],
    }

    def find_col(row_dict, aliases):
        for a in aliases:
            if a in row_dict: return row_dict[a]
        # Case-insensitive / partial match for composite headers like "student_id / رقم الطالب"
        for k, v in row_dict.items():
            k_clean = str(k).strip().lower()
            for a in aliases:
                a_clean = a.strip().lower()
                if a_clean == k_clean or a_clean in k_clean:
                    return v
        return ""

    results = {"created": 0, "skipped": 0, "errors": [], "preview": []}

    for i, row in enumerate(rows[:500], start=2):  # max 500 rows
        sid       = to_eng(find_col(row, COL_MAP["student_id"]).strip())
        full_name = find_col(row, COL_MAP["full_name"]).strip()
        year      = to_eng(find_col(row, COL_MAP["year"]).strip())
        college   = find_col(row, COL_MAP["college"]).strip()
        email     = find_col(row, COL_MAP["email"]).strip().lower()

        if not sid or not full_name or not email:
            results["errors"].append(f"سطر {i}: رقم الطالب أو الاسم أو البريد الإلكتروني مفقود (البريد إلزامي)")
            results["skipped"] += 1
            continue

        # Validate college
        if college and college not in COLLEGES:
            # Try partial match
            matched = next((c for c in COLLEGES if college in c or c in college), None)
            if matched:
                college = matched
            else:
                college = COLLEGES[0]  # fallback

        # admin restricted to own college
        if session.get("role") == "admin" and session.get("college"):
            college = session.get("college")

        if not year or not re.fullmatch(r"\d{4}", year):
            year = sid[:4] if len(sid) >= 4 else str(CURRENT_YEAR)

        # Check if already exists
        db  = get_db(); cur = db.cursor()
        cur.execute(f"SELECT id FROM students WHERE student_id={ph()}", (sid,))
        if cur.fetchone():
            db.close()
            results["skipped"] += 1
            results["preview"].append({"sid": sid, "name": full_name, "status": "موجود مسبقاً"})
            continue

        # Generate temp password
        temp_pw     = secrets.token_urlsafe(8)
        placeholder_img = os.path.join(UPLOAD_FOLDER, "placeholder.jpg")

        # Create a placeholder image if needed
        if not os.path.exists(placeholder_img):
            try:
                from PIL import Image, ImageDraw
                img_ph = Image.new("RGB", (400, 500), color=(26, 58, 107))
                draw   = ImageDraw.Draw(img_ph)
                draw.rectangle([160, 100, 240, 180], fill=(232, 184, 75))
                img_ph.save(placeholder_img, "JPEG")
            except Exception:
                pass

        # Save placeholder in correct folder
        from image_processor import _college_folder
        col_folder = _college_folder(college)
        img_dir    = os.path.join(UPLOAD_FOLDER, year, col_folder)
        os.makedirs(img_dir, exist_ok=True)
        img_name   = f"{sid}_pending.jpg"
        img_path   = os.path.join(img_dir, img_name)
        rel_path   = f"uploads/{year}/{col_folder}/{img_name}"

        if os.path.exists(placeholder_img):
            import shutil
            shutil.copy2(placeholder_img, img_path)
        else:
            with open(img_path, "wb") as fh:
                fh.write(b"")

        # Insert student record
        try:
            uid = session.get("user_id")
            cur.execute(
                f"INSERT INTO students (student_id,full_name,year,college,email,image_path,registered_by) VALUES ({','.join([ph()]*7)})",
                (sid, full_name, year, college, email or None, rel_path, uid)
            )
            db.commit()

            # ── Create student user account ──
            # Email = student_id@domain  (e.g. 2024001001@university.edu.eg)
            student_login_email = f"{sid}@{UNIVERSITY_DOMAIN}"
            temp_pw             = sid
            hashed_pw           = hash_pw(temp_pw)

            cur.execute(f"SELECT id FROM users WHERE email={ph()} OR student_id={ph()}", (student_login_email, sid))
            existing_user = cur.fetchone()
            if not existing_user:
                cur.execute(
                    f"INSERT INTO users (email,password_hash,password_plain,full_name,role,college,student_id,is_active,email_verified) VALUES ({','.join([ph()]*9)})",
                    (student_login_email, hashed_pw, temp_pw, full_name, "student",
                     college, sid, True if is_use_pg() else 1, True if is_use_pg() else 1)   # pre-verified, active
                )
                db.commit()
            else:
                user_id_val = existing_user["id"] if isinstance(existing_user, dict) else existing_user[0]
                cur.execute(
                    f"UPDATE users SET password_hash={ph()}, password_plain={ph()}, student_id={ph()}, full_name={ph()}, college={ph()}, is_active={ph()}, email_verified={ph()} WHERE id={ph()}",
                    (hashed_pw, temp_pw, sid, full_name, college, True if is_use_pg() else 1, True if is_use_pg() else 1, user_id_val)
                )
                db.commit()

            results["created"] += 1
            results["preview"].append({
                "sid":      sid,
                "name":     full_name,
                "status":   "تم الإنشاء",
                "email":    student_login_email,
                "temp_pw":  temp_pw,
            })
            log_action(uid, "BULK_IMPORT_STUDENT", target=sid,
                       detail=full_name, ip=request.remote_addr)
        except Exception as e:
            results["errors"].append(f"سطر {i} ({sid}): {e}")
            results["skipped"] += 1
            results["preview"].append({"sid": sid, "name": full_name, "status": "خطأ"})
        finally:
            db.close()

        # Send welcome email with login credentials
        if email:
            card_link   = url_for("student_card", student_id=sid, _external=True)
            login_email = f"{sid}@{UNIVERSITY_DOMAIN}"
            _send_student_welcome(email, full_name, sid, login_email, temp_pw, card_link)

    return jsonify(success=True, results=results)


@app.route("/admin/bulk-import/template")
@role_required("superadmin")
def bulk_import_template():
    """Download a sample Excel template for bulk import."""
    wb = Workbook()
    ws = wb.active
    ws.title = "بيانات الطلاب"
    ws.sheet_view.rightToLeft = True

    hf = Font(name="Arial", bold=True, color="FFFFFF", size=12)
    hb = PatternFill("solid", fgColor="0D1F3C")
    ca = Alignment(horizontal="center", vertical="center")

    headers = ["student_id", "full_name", "year", "college", "email"]
    ar_headers = ["رقم الطالب", "الاسم الكامل", "السنة", "الكلية", "الإيميل"]
    widths = [18, 40, 8, 36, 34]

    for ci, (h, ah, w) in enumerate(zip(headers, ar_headers, widths), 1):
        cell = ws.cell(1, ci, f"{h} / {ah}")
        cell.font = hf; cell.fill = hb; cell.alignment = ca
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[1].height = 26

    # Sample rows
    samples = [
        ("2024001001", "محمد أحمد علي حسن",      "2024", "كلية الحاسبات والمعلومات", "student1@university.edu.eg"),
        ("2024001002", "فاطمة عبدالله إبراهيم سيد","2024", "كلية الطب البشري",         ""),
        ("2023005010", "أحمد محمود خالد عمر",      "2023", "كلية الهندسة",             "student3@university.edu.eg"),
    ]
    alt = PatternFill("solid", fgColor="EBF2FA")
    for ri, row in enumerate(samples, 2):
        for ci, v in enumerate(row, 1):
            cell = ws.cell(ri, ci, v)
            cell.alignment = ca
            if ri % 2 == 0: cell.fill = alt
        ws.row_dimensions[ri].height = 20

    ws.freeze_panes = "A2"
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name); tmp.close()
    return send_file(tmp.name, as_attachment=True,
                     download_name="students_import_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")



def _send_student_welcome(to: str, name: str, student_id: str,
                          login_email: str, temp_pw: str, card_link: str):
    """Send welcome email with login credentials to newly imported student."""
    html = f"""
<div dir="rtl" style="font-family:Cairo,Arial;max-width:540px;margin:auto">
  <div style="background:linear-gradient(135deg,#0d1f3c,#1a3a6b);padding:30px;border-radius:14px 14px 0 0;text-align:center">
    <h1 style="color:#e8b84b;margin:0;font-size:1.4rem">&#127891; مرحباً بك في النظام الجامعي</h1>
  </div>
  <div style="background:#f0f4f9;padding:28px;border-radius:0 0 14px 14px">
    <p style="font-size:1rem">أهلاً <strong>{name}</strong>،</p>
    <p style="margin-top:8px;color:#444">تم تسجيلك في نظام القيد الجامعي.</p>

    <div style="background:#0d1f3c;border-radius:10px;padding:16px;margin:16px 0">
      <p style="color:rgba(255,255,255,.5);font-size:.8rem;margin-bottom:8px;text-align:center">رقمك الجامعي</p>
      <p style="color:#e8b84b;font-family:monospace;font-size:1.5rem;font-weight:700;letter-spacing:3px;text-align:center">{student_id}</p>
    </div>

    <div style="background:#fff;border:1px solid #dce3ef;border-radius:10px;padding:16px;margin:14px 0">
      <p style="font-weight:700;color:#1a2744;margin-bottom:10px">بيانات تسجيل الدخول:</p>
      <p style="font-size:.88rem;color:#444;margin-bottom:5px">البريد: <strong style="direction:ltr;display:inline-block">{login_email}</strong></p>
      <p style="font-size:.88rem;color:#444">كلمة المرور المؤقتة: <strong style="font-family:monospace;letter-spacing:1px">{temp_pw}</strong></p>
      <p style="font-size:.75rem;color:#c53030;margin-top:8px">* يُنصح بتغيير كلمة المرور بعد أول دخول</p>
    </div>

    <a href="{card_link}" style="display:inline-block;background:#1a3a6b;color:#e8b84b;
      padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:1rem;margin:10px 0">
      فتح بطاقة الهوية ورفع الصورة
    </a>
  </div>
</div>"""
    send_email(to, "مرحباً – تم تسجيلك في النظام الجامعي", html)


# ══════════════════════════════════════════════════════════════════════════
# STUDENT SELF-REGISTRATION  (student role only — simple dedicated page)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/student/register", methods=["GET"])
@login_required
def student_self_register():
    """Dedicated registration page for students — only requires College & Photo."""
    if session.get("role") != "student":
        return redirect(url_for("register_form"))

    uid = session.get("user_id")
    db  = get_db(); cur = db.cursor()
    cur.execute(f"SELECT id, email, full_name, student_id FROM users WHERE id={ph()}", (uid,))
    u = _row_to_dict(cur.fetchone())

    email     = u.get("email") or session.get("email", "")
    full_name = u.get("full_name") or session.get("user_name", "")

    stu_info = extract_student_info_from_email(email)
    sid      = u.get("student_id") or stu_info.get("student_id") or session.get("student_id", "")
    year     = stu_info.get("year") or (sid[:4] if len(sid) >= 4 else "")
    code     = stu_info.get("code") or (sid[4:] if len(sid) > 4 else "")

    # Auto-save extracted student_id to user record if not saved yet
    if sid and not u.get("student_id"):
        cur.execute(f"UPDATE users SET student_id={ph()} WHERE id={ph()}", (sid, uid))
        db.commit()
        session["student_id"] = sid

    # Already registered in students table → go straight to card
    if sid:
        cur.execute(f"SELECT student_id FROM students WHERE student_id={ph()}", (sid,))
        if cur.fetchone():
            db.close()
            return redirect(url_for("student_card", student_id=sid))
    db.close()

    return render_template("student_self_register.html",
                           colleges=COLLEGES,
                           user_name=full_name,
                           user_email=email,
                           student_id=sid,
                           student_year=year,
                           student_code=code)


@app.route("/student/register", methods=["POST"])
@login_required
@limiter.limit(lambda: os.getenv("LIMIT_STUDENT_REGISTER", "30 per hour"))
def student_self_register_post():
    """Handle student self-registration form submission — only requires College & Photo."""
    if session.get("role") != "student":
        return jsonify(success=False, message="غير مسموح"), 403
    try:
        uid = session.get("user_id")
        db  = get_db(); cur = db.cursor()
        cur.execute(f"SELECT id, email, full_name, student_id FROM users WHERE id={ph()}", (uid,))
        u = _row_to_dict(cur.fetchone())
        db.close()

        email     = u.get("email") or session.get("email", "")
        full_name = u.get("full_name") or session.get("user_name", "") or request.form.get("full_name","").strip()

        # Extract student info automatically from email or database
        stu_info   = extract_student_info_from_email(email)
        student_id = u.get("student_id") or stu_info.get("student_id") or session.get("student_id") or to_eng(request.form.get("student_id","").strip())
        year       = stu_info.get("year") or (student_id[:4] if len(student_id) >= 4 else to_eng(request.form.get("year","").strip()))
        code       = stu_info.get("code") or (student_id[4:] if len(student_id) > 4 else to_eng(request.form.get("code","").strip()))

        college    = request.form.get("college","").strip()
        rotation   = int(request.form.get("rotation","0"))
        flip_h     = request.form.get("flip_h","") == "1"
        zoom       = float(request.form.get("zoom","1.0"))
        offset_x   = float(request.form.get("offset_x","0.0"))
        offset_y   = float(request.form.get("offset_y","0.0"))
        auto_crop  = request.form.get("auto_crop","1") == "1"
        image_file = request.files.get("image")

        if not student_id or not year or not code:
            return jsonify(success=False, message="تعذر استخراج كود الطالب وسنة القيد من البريد الجامعي"), 400

        if not full_name:
            return jsonify(success=False, message="اسم الطالب غير مسجل"), 400

        if college not in COLLEGES:
            return jsonify(success=False, message="يرجى اختيار كليتك من القائمة"), 400

        # Check duplicate
        db  = get_db(); cur = db.cursor()
        cur.execute(f"SELECT student_id, full_name, image_path FROM students WHERE student_id={ph()}", (student_id,))
        existing = _row_to_dict(cur.fetchone()); db.close()
        if existing:
            img_url = None
            if existing.get("image_path"):
                p = existing["image_path"]
                img_url = p if p.startswith("http") else f"/static/{p}"
            return jsonify(
                success=False, duplicate=True,
                existing_name=existing["full_name"],
                existing_img=img_url,
                existing_id=existing["student_id"],
                message=f"الرقم {student_id} مسجل مسبقاً باسم: {existing['full_name']}"
            ), 409

        if not image_file or not image_file.filename.lower().endswith((".jpg",".jpeg",".png")):
            return jsonify(success=False, message="يرجى التقاط أو رفع صورتك الشخصية"), 400

        raw = image_file.read()
        if len(raw) > app.config["MAX_CONTENT_LENGTH"]:
            return jsonify(success=False, message="حجم الصورة يتجاوز 5 MB"), 400

        # High-performance single-pass image processing and face validation
        ok, face_msg, processed = process_and_validate_photo(
            raw, rotation=rotation, flip_h=flip_h,
            zoom=zoom, offset_x=offset_x, offset_y=offset_y,
            auto_crop=auto_crop
        )
        if not ok:
            return jsonify(success=False, message=face_msg), 400

        # Save directly to local VPS storage
        result = save_image(processed, student_id, year, college, UPLOAD_FOLDER, skip_validation=True)

        db  = get_db(); cur = db.cursor()
        try:
            cur.execute(
                f"INSERT INTO students (student_id,full_name,year,college,email,image_path,registered_by) VALUES ({','.join([ph()]*7)})",
                (student_id, full_name, year, college,
                 email, result["path"], uid)
            )
            # Link student_id to user account
            cur.execute(f"UPDATE users SET student_id={ph()} WHERE id={ph()}",
                        (student_id, uid))
            db.commit()
            log_action(uid, "STUDENT_SELF_REGISTER", target=student_id,
                       detail=full_name, ip=request.remote_addr)
        except Exception as e:
            db.close()
            if "UNIQUE" in str(e) or "unique" in str(e).lower():
                return jsonify(success=False,
                    message=f"الرقم {student_id} مسجل مسبقاً", duplicate=True), 409
            raise
        db.close()

        # Update session
        session["student_id"] = student_id

        return jsonify(success=True,
            message="تم تسجيل بياناتك بنجاح!",
            card_url=url_for("student_card", student_id=student_id),
            image_url=result["url"]), 201

    except Exception:
        app.logger.exception("Student self-register error")
        return jsonify(success=False, message="حدث خطأ داخلي أثناء تسجيل البيانات"), 500


# ══════════════════════════════════════════════════════════════════════════
# GOOGLE DRIVE OAUTH GATEWAY
# ══════════════════════════════════════════════════════════════════════════

@app.route("/admin/gdrive/auth")
@login_required
def gdrive_auth():
    if session.get("role") != "superadmin":
        abort(403)
    
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        return "Error: GOOGLE_CLIENT_ID is not configured in .env", 400
        
    redirect_uri = f"{request.scheme}://{request.host}/admin/gdrive/callback"
    
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&scope=https://www.googleapis.com/auth/drive"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return redirect(auth_url)


@app.route("/admin/gdrive/callback")
@login_required
def gdrive_callback():
    if session.get("role") != "superadmin":
        abort(403)
        
    code = request.args.get("code")
    if not code:
        err = request.args.get("error")
        return f"Google OAuth Error: {err or 'Missing authorization code'}", 400
        
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        return "Error: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET is not configured in .env", 400
        
    redirect_uri = f"{request.scheme}://{request.host}/admin/gdrive/callback"
    token_url = "https://oauth2.googleapis.com/token"
    
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    
    try:
        import requests
        res = requests.post(token_url, data=data, timeout=15)
        if res.status_code == 200:
            tokens = res.json()
            refresh_token = tokens.get("refresh_token")
            
            html = f"""
            <div dir="rtl" style="font-family:Cairo,Arial,sans-serif;max-width:600px;margin:50px auto;padding:30px;border:1px solid #dce3ef;border-radius:14px;background:#f0f4f9;box-shadow:0 8px 30px rgba(0,0,0,0.05)">
              <h1 style="color:#1a73e8;margin-top:0">🎉 تم الاتصال بـ Google Drive بنجاح!</h1>
              <p style="color:#4a5568;line-height:1.6">تم الحصول على رمز التحديث (Refresh Token) بنجاح. يرجى نسخه ووضعه في ملف <strong>.env</strong> الخاص بالتطبيق:</p>
              
              <div style="background:#2d3748;color:#fff;padding:16px;border-radius:8px;font-family:monospace;font-size:0.9rem;word-break:break-all;margin:20px 0;user-select:all" title="انقر لتحديد الكل">
                GOOGLE_REFRESH_TOKEN={refresh_token}
              </div>
              
              <p style="color:#e53e3e;font-size:0.85rem;font-weight:bold">* تنبيه: هذا الرمز سري للغاية ويسمح بالوصول لملفاتك، لا تشاركه مع أي شخص.</p>
              <p style="color:#718096;font-size:0.8rem">بعد تعديل ملف .env، أعد تشغيل السيرفر لتفعيل مزامنة الصور تلقائياً.</p>
              <a href="/" style="display:inline-block;margin-top:20px;padding:10px 20px;background:#1a73e8;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold">الذهاب للوحة التحكم</a>
            </div>
            """
            return html
        else:
            return f"Failed to obtain token from Google: {res.text}", 400
    except Exception as e:
        return f"Connection error: {e}", 500



@app.errorhandler(403)
def forbidden(e):
    if request.is_json or request.path.startswith(("/api/", "/admin/")):
        return jsonify(success=False, message="غير مصرح: هذه العملية تتطلب صلاحيات أعلى"), 403
    return render_template("auth_message.html",
        title="ليس لديك صلاحية",
        msg="هذه الصفحة تتطلب صلاحيات أعلى.", type="error"), 403

@app.errorhandler(404)
def not_found(e):
    if request.is_json or request.path.startswith(("/api/", "/admin/")):
        return jsonify(success=False, message="العنصر أو الصفحة المطلوبة غير موجودة"), 404
    return render_template("auth_message.html",
        title="الصفحة غير موجودة",
        msg="تأكد من الرابط وحاول مجدداً.", type="error"), 404

@app.errorhandler(413)
def request_entity_too_large(e):
    if request.is_json or request.path.startswith(("/api/", "/admin/")):
        return jsonify(success=False, message="حجم الملف كبير جداً. الحد الأقصى المسموح به هو 5 ميجابايت."), 413
    return render_template("auth_message.html",
        title="حجم الملف كبير جداً",
        msg="الحد الأقصى المسموح به للملفات هو 5 ميجابايت. يرجى اختيار ملف أصغر حجماً.", type="error"), 413

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"[Server Error 500] {e}")
    if request.is_json or request.path.startswith(("/api/", "/admin/")):
        return jsonify(success=False, message="حدث خطأ داخلي في الخادم. يرجى المحاولة لاحقاً."), 500
    return render_template("auth_message.html",
        title="خطأ في الخادم",
        msg="حدث خطأ غير متوقع أثناء معالجة طلبك. يرجى المحاولة مرة أخرى لاحقاً.", type="error"), 500


# ── run ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="0.0.0.0", port=5000)