"""
security_middleware.py – Enterprise-Grade Web Application Security Shield.

Provides active request inspection, exploit mitigation, and security response headers:
1. Method Guard: Rejects non-standard/dangerous HTTP methods (TRACE, TRACK, CONNECT).
2. Scanner & Bot Repellent: Blocks automated vulnerability tools (sqlmap, nikto, acunetix, etc.).
3. Probe Blocker: Immediately drops scanning requests targeting sensitive files (.env, wp-login, etc.).
4. SQL Injection (SQLi) Filter: Detects and stops destructive SQL injection signatures.
5. Cross-Site Scripting (XSS) Filter: Prevents malicious script payload execution.
6. Path Traversal & LFI Guard: Blocks directory traversal sequences (../, ..\\, %00, null bytes).
7. Brute-Force Rate Limiter: Tracks repeated failed login attempts per client IP.
8. Defense Response Headers: CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy.
"""

import re
import time
import logging
from collections import defaultdict
from flask import request, abort, jsonify, Response

logger = logging.getLogger("security_shield")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# ── 1. Disallowed HTTP Methods ───────────────────────────────────────────────
DISALLOWED_METHODS = {"TRACE", "TRACK", "CONNECT", "DEBUG"}

# ── 2. Malicious Scanner Signatures (User-Agent) ──────────────────────────────
SCANNER_UA_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"sqlmap",
        r"nikto",
        r"acunetix",
        r"nessus",
        r"havij",
        r"masscan",
        r"nmap",
        r"zgrab",
        r"gobuster",
        r"dirbuster",
        r"wpscan",
        r"burpcollaborator",
        r"arachni",
        r"netsparker",
        r"openvas",
        r"w3af",
    ]
]

# ── 3. Probing Targets for Sensitive Files / Admin Portals ───────────────────
SENSITIVE_PROBE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"(?:^|/)\.env(?:$|[/?])",
        r"(?:^|/)\.git(?:$|[/?])",
        r"(?:^|/)\.aws(?:$|[/?])",
        r"(?:^|/)wp-(?:login|admin|config)\.php",
        r"(?:^|/)xmlrpc\.php",
        r"(?:^|/)eval-stdin\.php",
        r"(?:^|/)phpmyadmin",
        r"(?:^|/)pma(?:$|/)",
        r"(?:^|/)web\.config",
        r"(?:^|/)id_rsa",
        r"(?:^|/)etc/passwd",
        r"(?:^|/)win\.ini",
        r"(?:^|/)shell\.php",
    ]
]

# ── 4. SQL Injection Patterns ─────────────────────────────────────────────────
# Patterns designed to catch classic SQLi exploits while preserving normal Arabic / English names
SQLI_PATTERNS = [
    re.compile(r"\bUNION\b(?:\s+|/\*.*?\*/)+\bSELECT\b", re.IGNORECASE),
    re.compile(r"\b(?:SLEEP|BENCHMARK|PG_SLEEP)\s*\(\s*\d+\s*\)", re.IGNORECASE),
    re.compile(r"\bWAITFOR\s+DELAY\b", re.IGNORECASE),
    re.compile(r"\bXP_CMDSHELL\b", re.IGNORECASE),
    re.compile(r"(?:'|\")\s*(?:OR|AND)\s+['\"]?1['\"]?\s*=\s*['\"]?1", re.IGNORECASE),
    re.compile(r"(?:'|\")\s*(?:OR|AND)\s+1=1\s*(?:--|#|/\*)", re.IGNORECASE),
    re.compile(r";\s*(?:DROP|DELETE|TRUNCATE|ALTER)\s+(?:TABLE|DATABASE)\b", re.IGNORECASE),
    re.compile(r"\binformation_schema\.(?:tables|columns|schemata)\b", re.IGNORECASE),
]

# ── 5. Cross-Site Scripting (XSS) Patterns ───────────────────────────────────
XSS_PATTERNS = [
    re.compile(r"<\s*script\b[^>]*>", re.IGNORECASE),
    re.compile(r"javascript\s*:", re.IGNORECASE),
    re.compile(r"vbscript\s*:", re.IGNORECASE),
    re.compile(r"<\s*(?:iframe|embed|object)\b", re.IGNORECASE),
    re.compile(r"<\s*svg\b[^>]*\bonload\s*=", re.IGNORECASE),
    re.compile(r"\bon(?:error|load|click|mouseover|focus|blur)\s*=\s*['\"][^'\"]*alert\s*\(", re.IGNORECASE),
    re.compile(r"data\s*:\s*text/html", re.IGNORECASE),
]

# ── 6. Directory Traversal / LFI Patterns ────────────────────────────────────
PATH_TRAVERSAL_PATTERNS = [
    re.compile(r"\.\.[/\\]"),
    re.compile(r"%2e%2e(?:%2f|%5c|[/\\])", re.IGNORECASE),
    re.compile(r"%252e%252e", re.IGNORECASE),
    re.compile(r"(?:\x00|%00)"),
]

# ── 7. Brute-Force Rate Limiter (In-Memory Sliding Window) ───────────────────
class BruteForceProtector:
    def __init__(self, max_failures: int = 15, window_seconds: int = 600, block_seconds: int = 900):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self.failures = defaultdict(list)
        self.blocked_ips = {}

    def is_blocked(self, ip: str) -> bool:
        now = time.time()
        # Check explicit block expiry
        if ip in self.blocked_ips:
            if now < self.blocked_ips[ip]:
                return True
            else:
                del self.blocked_ips[ip]

        # Clean old failure timestamps
        timestamps = [t for t in self.failures.get(ip, []) if now - t < self.window_seconds]
        self.failures[ip] = timestamps
        if len(timestamps) >= self.max_failures:
            self.blocked_ips[ip] = now + self.block_seconds
            logger.warning(f"🚨 [SECURITY] IP {ip} temporarily blocked for {self.block_seconds}s due to repeated failures.")
            return True
        return False

    def record_failure(self, ip: str):
        now = time.time()
        self.failures[ip].append(now)
        if len(self.failures[ip]) >= self.max_failures:
            self.blocked_ips[ip] = now + self.block_seconds
            logger.warning(f"🚨 [SECURITY] IP {ip} exceeded max failures ({self.max_failures}). Temporarily blocked.")

    def record_success(self, ip: str):
        if ip in self.failures:
            del self.failures[ip]
        if ip in self.blocked_ips:
            del self.blocked_ips[ip]


brute_protector = BruteForceProtector()


def _inspect_value_recursively(val) -> str:
    """Recursively checks string/dict/list values against attack signatures."""
    if isinstance(val, str):
        # 1. Path traversal check
        for pt in PATH_TRAVERSAL_PATTERNS:
            if pt.search(val):
                return "Path Traversal attempt"

        # 2. SQLi check
        for sqli in SQLI_PATTERNS:
            if sqli.search(val):
                return "SQL Injection signature"

        # 3. XSS check
        for xss in XSS_PATTERNS:
            if xss.search(val):
                return "Cross-Site Scripting (XSS) payload"

    elif isinstance(val, dict):
        for k, v in val.items():
            err = _inspect_value_recursively(k) or _inspect_value_recursively(v)
            if err:
                return err
    elif isinstance(val, (list, tuple)):
        for item in val:
            err = _inspect_value_recursively(item)
            if err:
                return err
    return ""


def init_security_middleware(app):
    """
    Hooks enterprise-grade security checks into Flask request lifecycle.
    """

    @app.before_request
    def security_shield_before_request():
        # 1. Method check
        if request.method in DISALLOWED_METHODS:
            logger.warning(f"🚫 [SECURITY] Blocked disallowed HTTP method: {request.method} from {request.remote_addr}")
            abort(405)

        # 2. Scanner / Bot check
        user_agent = request.headers.get("User-Agent", "")
        for pattern in SCANNER_UA_PATTERNS:
            if pattern.search(user_agent):
                logger.warning(f"🚫 [SECURITY] Blocked malicious scanner UA: '{user_agent}' from {request.remote_addr}")
                return jsonify(error="Access Denied: Malicious scanner signature detected"), 403

        # 3. Exploit / Sensitive Path Probing
        req_path = request.path
        for pattern in SENSITIVE_PROBE_PATTERNS:
            if pattern.search(req_path):
                logger.warning(f"🚫 [SECURITY] Blocked probe to sensitive target: '{req_path}' from {request.remote_addr}")
                abort(404)

        # 4. Path Traversal in URL Path
        for pattern in PATH_TRAVERSAL_PATTERNS:
            if pattern.search(req_path):
                logger.warning(f"🚫 [SECURITY] Blocked path traversal attempt in path: '{req_path}' from {request.remote_addr}")
                return jsonify(error="Bad Request: Directory traversal prohibited"), 400

        # 5. Rate limiting on sensitive auth endpoints
        if request.endpoint in ("auth_login", "login", "student_login", "auth_forgot") and request.method == "POST":
            client_ip = request.remote_addr or "127.0.0.1"
            if brute_protector.is_blocked(client_ip):
                logger.warning(f"🚫 [SECURITY] Throttled brute-force attempt on login from {client_ip}")
                return jsonify(
                    error="تم حظر الطلبات مؤقتاً لكثرة المحاولات الفاشلة. يرجى الانتظار بضع دقائق ثم المحاولة مجدداً."
                ), 429

        # 6. Deep payload inspection (args, form, json)
        # Check query parameters
        for key, value in request.args.items():
            violation = _inspect_value_recursively(value) or _inspect_value_recursively(key)
            if violation:
                logger.warning(f"🚫 [SECURITY] Blocked {violation} in query param '{key}' from {request.remote_addr}")
                return jsonify(error=f"طلب غير صالح: تم اكتشاف محتوى غير آمن ({violation})"), 400

        # Check form data (excluding file uploads)
        if request.form:
            for key, value in request.form.items():
                violation = _inspect_value_recursively(value)
                if violation:
                    logger.warning(f"🚫 [SECURITY] Blocked {violation} in form field '{key}' from {request.remote_addr}")
                    return jsonify(error=f"طلب غير صالح: تم اكتشاف محتوى غير آمن ({violation})"), 400

        # Check JSON body if present
        if request.is_json:
            try:
                data = request.get_json(silent=True)
                if data:
                    violation = _inspect_value_recursively(data)
                    if violation:
                        logger.warning(f"🚫 [SECURITY] Blocked {violation} in JSON body from {request.remote_addr}")
                        return jsonify(error=f"طلب غير صالح: تم اكتشاف محتوى غير آمن ({violation})"), 400
            except Exception:
                pass

    @app.after_request
    def security_headers_after_request(response: Response):
        # 1. Prevent Clickjacking
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")

        # 2. Prevent MIME type sniffing
        response.headers.setdefault("X-Content-Type-Options", "nosniff")

        # 3. Cross-Site Scripting Filter for older browsers
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")

        # 4. Strict Referrer Policy
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

        # 5. Device Permissions Policy
        response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(self), microphone=()")

        # 6. Content-Security-Policy (Allow trusted CDNs and inline styles for UI)
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' blob:; "
            "frame-ancestors 'self';"
        )
        response.headers.setdefault("Content-Security-Policy", csp)

        # 7. Strict-Transport-Security (HTTPS only)
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

        return response

    print("[INFO] Security Defense Middleware successfully initialized.")
