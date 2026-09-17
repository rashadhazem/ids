"""
tests/test_security_and_auth.py
Comprehensive test suite for:
1. Complete password hiding (no plain text passwords stored, returned, or exported).
2. Unified login (/login, /auth/login, /student/login) for both students (by ID) and staff (by email).
3. Universal password reset for students by supervisors with college isolation.
4. Security middleware defenses (SQLi, scanner bots, probe blocking, path traversal, security headers).
"""
import sys, os, re, json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import unittest
import bcrypt
from app import app, hash_pw, check_pw
from database import get_db, ph

class TestSecurityAndAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        # Create clean test records
        cls.student_col = "كلية الهندسة"
        cls.other_col = "كلية طب الأسنان"
        cls.sid = "20269901"
        cls.other_sid = "20269902"
        cls.s_email = "sec_student@bua.edu.eg"
        cls.s_name = "طالب أمان تجريبي"
        cls.s_pw = "InitialP@ss123"

        cls.staff_email = "eng_supervisor@bua.edu.eg"
        cls.staff_name = "مشرف الهندسة"
        cls.staff_pw = "SupervisorP@ss123"

        db = get_db()
        cur = db.cursor()
        # Clean previous
        cur.execute(f"DELETE FROM audit_log WHERE target IN ({ph()},{ph()},{ph()},{ph()})", 
                    (cls.sid, cls.other_sid, cls.s_email, cls.staff_email))
        cur.execute(f"DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()}))", 
                    (cls.sid, cls.other_sid, cls.s_email, cls.staff_email))
        cur.execute(f"DELETE FROM students WHERE student_id IN ({ph()},{ph()})", (cls.sid, cls.other_sid))
        cur.execute(f"DELETE FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()})", 
                    (cls.sid, cls.other_sid, cls.s_email, cls.staff_email))
        db.commit()
        
        # Insert students
        cur.execute(
            f"INSERT INTO students (student_id, full_name, year, college, email, image_path) VALUES ({','.join([ph()]*6)})",
            (cls.sid, cls.s_name, "2026", cls.student_col, cls.s_email, "uploads/test.jpg")
        )
        cur.execute(
            f"INSERT INTO students (student_id, full_name, year, college, email, image_path) VALUES ({','.join([ph()]*6)})",
            (cls.other_sid, "طالب كلية أخرى", "2026", cls.other_col, "other_student@bua.edu.eg", "uploads/test2.jpg")
        )
        # Fetch sid primary keys
        cur.execute(f"SELECT id FROM students WHERE student_id={ph()}", (cls.sid,))
        r1 = cur.fetchone()
        cls.student_db_id = r1["id"] if isinstance(r1, dict) else r1[0]

        cur.execute(f"SELECT id FROM students WHERE student_id={ph()}", (cls.other_sid,))
        r2 = cur.fetchone()
        cls.other_student_db_id = r2["id"] if isinstance(r2, dict) else r2[0]

        # Insert student user
        s_hash = hash_pw(cls.s_pw)
        cur.execute(
            f"INSERT INTO users (email, password_hash, full_name, role, college, student_id, is_active, email_verified) VALUES ({','.join([ph()]*8)})",
            (cls.s_email, s_hash, cls.s_name, "student", cls.student_col, cls.sid, True, True)
        )
        # Insert supervisor user
        staff_hash = hash_pw(cls.staff_pw)
        cur.execute(
            f"INSERT INTO users (email, password_hash, full_name, role, college, student_id, is_active, email_verified) VALUES ({','.join([ph()]*8)})",
            (cls.staff_email, staff_hash, cls.staff_name, "admin", cls.student_col, None, True, True)
        )
        db.commit()
        db.close()

    @classmethod
    def tearDownClass(cls):
        db = get_db()
        cur = db.cursor()
        cur.execute(f"DELETE FROM audit_log WHERE target IN ({ph()},{ph()},{ph()},{ph()})", 
                    (cls.sid, cls.other_sid, cls.s_email, cls.staff_email))
        cur.execute(f"DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()}))", 
                    (cls.sid, cls.other_sid, cls.s_email, cls.staff_email))
        cur.execute(f"DELETE FROM students WHERE student_id IN ({ph()},{ph()})", (cls.sid, cls.other_sid))
        cur.execute(f"DELETE FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()})", 
                    (cls.sid, cls.other_sid, cls.s_email, cls.staff_email))
        db.commit()
        db.close()

    def test_01_password_hiding_in_database_and_apis(self):
        """Ensure password is NEVER stored plain or returned in API responses."""
        db = get_db()
        cur = db.cursor()
        cur.execute(f"SELECT * FROM users WHERE student_id={ph()}", (self.sid,))
        row = cur.fetchone()
        u = dict(row) if hasattr(row, 'keys') else dict(zip([d[0] for d in cur.description], row))
        db.close()

        # password_plain must not exist in columns or must be None
        self.assertIsNone(u.get("password_plain"), "password_plain MUST be None or nonexistent")
        self.assertTrue(u["password_hash"].startswith("$2b$") or u["password_hash"].startswith("$2a$"))
        self.assertTrue(check_pw(self.s_pw, u["password_hash"]))

        # Verify /admin/users API does not expose passwords
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"
            res = client.get("/admin/users")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            users_list = data.get("users", [])
            for user in users_list:
                self.assertNotIn("password_plain", user)
                self.assertNotIn("password_hash", user)

    def test_02_unified_login_student_by_id(self):
        """Unified login (/login) authenticates student by Student ID."""
        with app.test_client() as client:
            res = client.post("/login", data={
                "identifier": self.sid,
                "password": self.s_pw
            }, follow_redirects=False)
            self.assertEqual(res.status_code, 302)
            self.assertIn(f"/student/{self.sid}", res.headers.get("Location"))

    def test_03_unified_login_student_by_email(self):
        """Unified login (/login) authenticates student by Email as well."""
        with app.test_client() as client:
            res = client.post("/login", data={
                "identifier": self.s_email,
                "password": self.s_pw
            }, follow_redirects=False)
            self.assertEqual(res.status_code, 302)
            self.assertIn(f"/student/{self.sid}", res.headers.get("Location"))

    def test_04_unified_login_staff_by_email(self):
        """Unified login (/login) authenticates staff/supervisor and redirects to /dashboard."""
        with app.test_client() as client:
            res = client.post("/login", data={
                "identifier": self.staff_email,
                "password": self.staff_pw
            }, follow_redirects=False)
            self.assertEqual(res.status_code, 302)
            self.assertIn("/dashboard", res.headers.get("Location"))

    def test_05_unified_login_invalid_password(self):
        """Unified login rejects incorrect passwords gracefully."""
        with app.test_client() as client:
            res = client.post("/login", data={
                "identifier": self.sid,
                "password": "WrongPassword999"
            })
            self.assertEqual(res.status_code, 401)
            self.assertIn("كلمة المرور غير صحيحة", res.get_data(as_text=True))

    def test_06_supervisor_reset_student_password(self):
        """Supervisor resets student's password; verifies college isolation and instant login."""
        # 1. College supervisor resets student in their own college
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 999
                sess["role"] = "admin"
                sess["college"] = self.student_col
                sess["user_name"] = self.staff_name

            res = client.post(f"/admin/student/{self.student_db_id}/reset-password", json={
                "new_password": "NewSecretPass2026!"
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("new_password"), "NewSecretPass2026!")

        # 2. Verify student can now login with the new password
        with app.test_client() as client:
            res = client.post("/login", data={
                "identifier": self.sid,
                "password": "NewSecretPass2026!"
            }, follow_redirects=False)
            self.assertEqual(res.status_code, 302)
            self.assertIn(f"/student/{self.sid}", res.headers.get("Location"))

        # 3. Verify college isolation: supervisor CANNOT reset student in another college
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 999
                sess["role"] = "admin"
                sess["college"] = self.student_col
                sess["user_name"] = self.staff_name

            res_forbidden = client.post(f"/admin/student/{self.other_student_db_id}/reset-password", json={
                "new_password": "HackerPass123"
            })
            self.assertEqual(res_forbidden.status_code, 403)
            data_err = res_forbidden.get_json()
            self.assertFalse(data_err.get("success"))

    def test_07_security_middleware_headers(self):
        """Verify standard security headers are injected into HTTP responses."""
        with app.test_client() as client:
            res = client.get("/login")
            self.assertIn("X-Frame-Options", res.headers)
            self.assertIn(res.headers["X-Frame-Options"], ["SAMEORIGIN", "DENY"])
            self.assertIn("X-Content-Type-Options", res.headers)
            self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("Content-Security-Policy", res.headers)
            self.assertIn("Referrer-Policy", res.headers)

    def test_08_security_middleware_bot_and_probe_blocking(self):
        """Verify middleware blocks scanners, bots, probes, and path traversal."""
        with app.test_client() as client:
            # 1. Malicious bot user agent -> 403 Forbidden
            res_bot = client.get("/login", headers={"User-Agent": "sqlmap/1.4.7#stable"})
            self.assertEqual(res_bot.status_code, 403)

            # 2. Sensitive file probe (.env) -> 404 stealth abort
            res_probe = client.get("/.env")
            self.assertEqual(res_probe.status_code, 404)

            # 3. Malicious SQLi in query string -> 400 Bad Request
            res_sqli = client.get("/login?search=1%20UNION%20SELECT%20*%20FROM%20users")
            self.assertEqual(res_sqli.status_code, 400)

            # 4. Path traversal attempt -> 400 Bad Request
            res_lfi = client.get("/login?file=../../../../windows/system32/cmd.exe")
            self.assertEqual(res_lfi.status_code, 400)

if __name__ == "__main__":
    unittest.main()
