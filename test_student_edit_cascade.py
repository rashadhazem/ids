"""
test_student_edit_cascade.py – Verification for full cascade on student edit
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app import app
from database import get_db, ph

def test_cascade():
    print("=" * 60)
    print("Testing Student Edit Cascade across the entire system...")
    print("=" * 60)

    old_id = "20261111"
    old_name = "طالب قديم للاختبار"
    old_col = "كلية طب الأسنان"
    old_year = "2026"
    old_email = "old_student@bua.edu.eg"

    new_id = "20262222"
    new_name = "طالب معدل ومحدث بالكامل"
    new_col = "كلية الصيدلة فارما D"
    new_year = "2026"
    new_email = "updated_student@bua.edu.eg"

    db = get_db()
    cur = db.cursor()
    # Cleanup previous test runs
    cur.execute(f"DELETE FROM students WHERE student_id IN ({ph()},{ph()})", (old_id, new_id))
    cur.execute(f"DELETE FROM users WHERE student_id IN ({ph()},{ph()}) OR email IN ({ph()},{ph()})", (old_id, new_id, old_email, new_email))
    db.commit()

    # 1. Insert initial student and user
    cur.execute(
        f"INSERT INTO students (student_id, full_name, year, college, email, image_path) VALUES ({','.join([ph()]*6)})",
        (old_id, old_name, old_year, old_col, old_email, "uploads/2026/كلية_طب_الأسنان/20261111.jpg")
    )
    cur.execute(f"SELECT id FROM students WHERE student_id={ph()}", (old_id,))
    row_sid = cur.fetchone()
    sid = row_sid["id"] if isinstance(row_sid, dict) else row_sid[0]

    cur.execute(
        f"INSERT INTO users (email, password_hash, password_plain, full_name, role, college, student_id, is_active, email_verified) VALUES ({','.join([ph()]*9)})",
        (old_email, "hash_old", old_id, old_name, "student", old_col, old_id, True, True)
    )
    db.commit()
    db.close()
    print(f"[*] Initial student inserted: ID={old_id}, Name={old_name}, College={old_col}")

    # 2. Call admin_edit_student via test client
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "superadmin"
            sess["user_name"] = "مدير النظام"

        # Generate valid CSRF token in session
        res_get = client.get("/admin")
        # Extract csrf token from meta tag
        import re
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', res_get.get_data(as_text=True))
        token = m.group(1) if m else ""
        print(f"[*] res_get status: {res_get.status_code}, token extracted: '{token}'")

        edit_payload = {
            "csrf_token": token,
            "student_id": new_id,
            "full_name": new_name,
            "college": new_col,
            "year": new_year,
            "email": new_email
        }
        res = client.post(f"/admin/student/{sid}/edit", data=edit_payload, headers={"X-CSRFToken": token})
        print(f"[*] Edit API response status: {res.status_code}")
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.data}"
        data = res.get_json()
        assert data.get("success") is True, f"Edit failed: {data}"
        print(f"[*] Edit API message: {data.get('message')}")

    # 3. Verify in `students` table
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT * FROM students WHERE id={ph()}", (sid,))
    r1 = cur.fetchone()
    row = dict(r1) if hasattr(r1, 'keys') else dict(zip([d[0] for d in cur.description], r1))
    print(f"[*] Students table after edit: {row}")
    assert row["student_id"] == new_id, f"students.student_id mismatch: {row['student_id']}"
    assert row["full_name"] == new_name, f"students.full_name mismatch: {row['full_name']}"
    assert row["college"] == new_col, f"students.college mismatch: {row['college']}"
    assert row["email"] == new_email, f"students.email mismatch: {row['email']}"

    # 4. Verify in `users` table
    cur.execute(f"SELECT * FROM users WHERE student_id={ph()}", (new_id,))
    r2 = cur.fetchone()
    u_row = dict(r2) if hasattr(r2, 'keys') else dict(zip([d[0] for d in cur.description], r2))
    print(f"[*] Users table after edit: {u_row}")
    assert u_row["student_id"] == new_id, "users.student_id mismatch"
    assert u_row["full_name"] == new_name, "users.full_name mismatch"
    assert u_row["college"] == new_col, "users.college mismatch"
    assert u_row["password_plain"] == new_id, "users.password_plain was not synced to new_id"
    db.close()

    # 5. Verify student card redirection from old_id to new_id
    with app.test_client() as client:
        res_old = client.get(f"/student/{old_id}")
        print(f"[*] Old card URL /student/{old_id} status: {res_old.status_code}, Location: {res_old.headers.get('Location')}")
        assert res_old.status_code == 302, f"Expected redirect, got {res_old.status_code}"
        assert f"/student/{new_id}" in res_old.headers.get("Location")

        res_new = client.get(f"/student/{new_id}")
        assert res_new.status_code == 200, f"Expected 200 on new student card, got {res_new.status_code}"
        print(f"[*] New card URL /student/{new_id} loaded successfully!")

    print("\n✅ SUCCESS: Student edit cascade is 100% synchronized everywhere!")

    # Cleanup
    db = get_db()
    cur = db.cursor()
    cur.execute(f"DELETE FROM students WHERE student_id IN ({ph()},{ph()})", (old_id, new_id))
    cur.execute(f"DELETE FROM users WHERE student_id IN ({ph()},{ph()})", (old_id, new_id))
    db.commit()
    db.close()

if __name__ == "__main__":
    test_cascade()
