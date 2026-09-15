"""
test_college_audit_isolation.py
Verifies that college supervisors only see activities of their own college's students.
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app import app
from database import get_db, log_action, ph

def test_audit_scoping():
    print("=" * 65)
    print("Testing College Supervisor Audit Log Isolation...")
    print("=" * 65)

    eng_col = "كلية الهندسة"
    dent_col = "كلية طب الأسنان"

    s1_id = "20268881"
    s1_name = "طالب هندسة تجريبي"
    s1_email = "eng_student_test@bua.edu.eg"

    s2_id = "20268882"
    s2_name = "طالب أسنان تجريبي"
    s2_email = "dent_student_test@bua.edu.eg"

    db = get_db(); cur = db.cursor()
    # Cleanup previous runs (delete from audit_log before users due to FK constraint)
    cur.execute(f"DELETE FROM audit_log WHERE target IN ({ph()},{ph()},{ph()},{ph()})", (s1_id, s2_id, s1_email, s2_email))
    cur.execute(f"DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()}))", (s1_id, s2_id, s1_email, s2_email))
    cur.execute(f"DELETE FROM students WHERE student_id IN ({ph()},{ph()})", (s1_id, s2_id))
    cur.execute(f"DELETE FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()})", (s1_id, s2_id, s1_email, s2_email))
    db.commit()

    # Insert students
    cur.execute(
        f"INSERT INTO students (student_id, full_name, year, college, email, image_path) VALUES ({','.join([ph()]*6)})",
        (s1_id, s1_name, "2026", eng_col, s1_email, "uploads/2026/كلية_الهندسة/20268881.jpg")
    )
    cur.execute(
        f"INSERT INTO students (student_id, full_name, year, college, email, image_path) VALUES ({','.join([ph()]*6)})",
        (s2_id, s2_name, "2026", dent_col, s2_email, "uploads/2026/كلية_طب_الأسنان/20268882.jpg")
    )

    # Insert user accounts for both students
    cur.execute(
        f"INSERT INTO users (email, password_hash, password_plain, full_name, role, college, student_id, is_active, email_verified) VALUES ({','.join([ph()]*9)})",
        (s1_email, "hash1", s1_id, s1_name, "student", eng_col, s1_id, True, True)
    )
    cur.execute(f"SELECT id FROM users WHERE email={ph()}", (s1_email,))
    u1_id = cur.fetchone()
    u1_id = u1_id["id"] if isinstance(u1_id, dict) else u1_id[0]

    cur.execute(
        f"INSERT INTO users (email, password_hash, password_plain, full_name, role, college, student_id, is_active, email_verified) VALUES ({','.join([ph()]*9)})",
        (s2_email, "hash2", s2_id, s2_name, "student", dent_col, s2_id, True, True)
    )
    cur.execute(f"SELECT id FROM users WHERE email={ph()}", (s2_email,))
    u2_id = cur.fetchone()
    u2_id = u2_id["id"] if isinstance(u2_id, dict) else u2_id[0]

    db.commit()
    db.close()

    # Log specific audit actions
    log_action(u1_id, "STUDENT_SELF_REGISTER", target=s1_id, detail="Engineering student registered", ip="127.0.0.1")
    log_action(u1_id, "UPDATE_PHOTO", target=s1_id, detail="Engineering student updated photo", ip="127.0.0.1")

    log_action(u2_id, "STUDENT_SELF_REGISTER", target=s2_id, detail="Dentistry student registered", ip="127.0.0.1")
    log_action(u2_id, "UPDATE_PHOTO", target=s2_id, detail="Dentistry student updated photo", ip="127.0.0.1")

    print("[*] Test data created: 2 students in different colleges with audit records.")

    with app.test_client() as client:
        # TEST 1: Engineering Supervisor
        print("\n--- Testing Engineering Supervisor ---")
        with client.session_transaction() as sess:
            sess["user_id"] = 9991
            sess["role"] = "admin"
            sess["college"] = eng_col
            sess["user_name"] = "مشرف كلية الهندسة"
            sess["email"] = "eng_admin@bua.edu.eg"

        res_audit = client.get("/admin/audit?page=1")
        assert res_audit.status_code == 200, f"Expected 200, got {res_audit.status_code}"
        data = res_audit.get_json()
        targets = [l.get("student_id") or l.get("target") for l in data.get("logs", [])]

        print(f"Engineering supervisor audit logs returned: {len(targets)} entries.")
        assert s1_id in targets, f"Engineering student {s1_id} should be visible to engineering supervisor"
        assert s2_id not in targets, f"Dentistry student {s2_id} MUST NOT be visible to engineering supervisor!"
        print("[PASS] Engineering supervisor sees Engineering student and NOT Dentistry student.")

        # Test Dashboard for Engineering Supervisor
        res_dash = client.get("/dashboard")
        assert res_dash.status_code == 200
        html = res_dash.data.decode("utf-8")
        assert s1_id in html or s1_name in html, "Engineering student activity should be rendered on dashboard"
        assert s2_id not in html, "Dentistry student activity MUST NOT be rendered on engineering dashboard"
        print("[PASS] Engineering dashboard correctly scoped to Engineering students only.")

        # TEST 2: Dentistry Supervisor
        print("\n--- Testing Dentistry Supervisor ---")
        with client.session_transaction() as sess:
            sess["user_id"] = 9992
            sess["role"] = "admin"
            sess["college"] = dent_col
            sess["user_name"] = "مشرف كلية طب الأسنان"
            sess["email"] = "dent_admin@bua.edu.eg"

        res_audit = client.get("/admin/audit?page=1")
        assert res_audit.status_code == 200
        data = res_audit.get_json()
        targets = [l.get("student_id") or l.get("target") for l in data.get("logs", [])]

        print(f"Dentistry supervisor audit logs returned: {len(targets)} entries.")
        assert s2_id in targets, f"Dentistry student {s2_id} should be visible to dentistry supervisor"
        assert s1_id not in targets, f"Engineering student {s1_id} MUST NOT be visible to dentistry supervisor!"
        print("[PASS] Dentistry supervisor sees Dentistry student and NOT Engineering student.")

        # Test Dashboard for Dentistry Supervisor
        res_dash = client.get("/dashboard")
        assert res_dash.status_code == 200
        html = res_dash.data.decode("utf-8")
        assert s2_id in html or s2_name in html, "Dentistry student activity should be rendered on dashboard"
        assert s1_id not in html, "Engineering student activity MUST NOT be rendered on dentistry dashboard"
        print("[PASS] Dentistry dashboard correctly scoped to Dentistry students only.")

        # TEST 3: Superadmin sees both
        print("\n--- Testing Superadmin ---")
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "superadmin"
            sess["college"] = None
            sess["user_name"] = "المدير العام"
            sess["email"] = "admin@bua.edu.eg"

        res_audit = client.get("/admin/audit?page=1")
        assert res_audit.status_code == 200
        data = res_audit.get_json()
        targets = [l.get("student_id") or l.get("target") for l in data.get("logs", [])]

        assert s1_id in targets, "Superadmin should see Engineering student"
        assert s2_id in targets, "Superadmin should see Dentistry student"
        print("[PASS] Superadmin sees activities from all colleges.")

    # Cleanup
    db = get_db(); cur = db.cursor()
    cur.execute(f"DELETE FROM audit_log WHERE target IN ({ph()},{ph()},{ph()},{ph()})", (s1_id, s2_id, s1_email, s2_email))
    cur.execute(f"DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()}))", (s1_id, s2_id, s1_email, s2_email))
    cur.execute(f"DELETE FROM students WHERE student_id IN ({ph()},{ph()})", (s1_id, s2_id))
    cur.execute(f"DELETE FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()})", (s1_id, s2_id, s1_email, s2_email))
    db.commit(); db.close()

    print("\n" + "=" * 65)
    print("ALL TESTS PASSED! AUDIT LOG ISOLATION IS 100% VERIFIED.")
    print("=" * 65)

if __name__ == "__main__":
    test_audit_scoping()
