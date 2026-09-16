"""
test_export_photos.py – Verification script for ZIP photo export functionality
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tempfile
import zipfile
from io import BytesIO
from PIL import Image

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app import app
from database import get_db, ph, is_use_pg

def run_test():
    print("=" * 60)
    print("Testing /admin/export-photos ZIP archive generation...")
    print("=" * 60)

    # 1. Ensure test photo exists in uploads folder
    test_year = "2026"
    test_college = "كلية طب الأسنان"
    test_sid = "20269999"
    test_name = "طالب_تجريبي_للاختبار"

    from image_processor import save_image
    from app import UPLOAD_FOLDER
    img = Image.new("RGB", (400, 500), color=(73, 109, 137))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    upload_root = os.path.join(app.root_path, "static", "uploads")
    save_res = save_image(img_bytes, test_sid, test_year, test_college, upload_root, skip_validation=True)
    print(f"[*] Test photo saved: {save_res['path']}")

    # 2. Insert test student into database
    db = get_db()
    cur = db.cursor()
    cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (test_sid,))
    db.commit()

    cur.execute(
        f"INSERT INTO students (student_id, full_name, year, college, email, image_path) VALUES ({','.join([ph()]*6)})",
        (test_sid, test_name, test_year, test_college, "test_student@bua.edu.eg", save_res["path"])
    )
    db.commit()
    db.close()
    print(f"[*] Test student record inserted in database.")

    # 3. Request /admin/export-photos with test client as superadmin
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "superadmin"
            sess["user_name"] = "مدير النظام"

        res = client.get(f"/admin/export-photos?college={test_college}&year={test_year}")
        print(f"[*] Response status: {res.status_code}")
        assert res.status_code == 200, f"Expected 200, got {res.status_code}"
        assert res.mimetype == "application/zip", f"Expected application/zip, got {res.mimetype}"

        # 4. Verify ZIP content
        zip_bytes = BytesIO(res.data)
        with zipfile.ZipFile(zip_bytes, "r") as zf:
            namelist = zf.namelist()
            print(f"[*] ZIP contains {len(namelist)} items: {namelist}")
            # Photo must be saved with student ID ONLY, without student name
            matching_files = [n for n in namelist if n.endswith(f"{test_sid}.jpg")]
            assert len(matching_files) > 0, f"Expected photo ending with '{test_sid}.jpg', found: {namelist}"
            # Ensure student name is NOT part of the filename
            assert not any(test_name in n for n in namelist if not n.endswith(".csv")), "Student name must NOT be in photo filename!"
            assert "manifest.csv" in namelist, "manifest.csv not found in ZIP!"

            # Check manifest content
            manifest = zf.read("manifest.csv").decode("utf-8-sig")
            print(f"[*] Manifest preview:\n{manifest}")
            assert test_sid in manifest

        # 5. Verify college supervisor (role 'admin') is BLOCKED with 403 Forbidden
        with client.session_transaction() as sess:
            sess["user_id"] = 2
            sess["role"] = "admin"
            sess["college"] = test_college
        res_admin = client.get(f"/admin/export-photos?college={test_college}&year={test_year}")
        print(f"[*] College supervisor access status: {res_admin.status_code}")
        assert res_admin.status_code == 403, f"Expected 403 for supervisor, got {res_admin.status_code}"

    print("\n✅ SUCCESS: /admin/export-photos passed all tests!")

    # Clean up test student and file
    db = get_db()
    cur = db.cursor()
    cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (test_sid,))
    db.commit()
    db.close()

    saved_full = os.path.normpath(os.path.join(upload_root, "..", save_res["path"]))
    if os.path.exists(saved_full):
        try:
            os.remove(saved_full)
        except Exception:
            pass

if __name__ == "__main__":
    run_test()
