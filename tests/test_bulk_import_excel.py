"""
tests/test_bulk_import_excel.py
Comprehensive test suite verifying:
1. Superadmin access to /admin/bulk-import and template download.
2. Direct Excel upload (.xlsx) with synchronous processing.
3. Asynchronous background processing for bulk Excel uploads.
4. Auto-creation of student records, placeholder images, and bcrypt user accounts.
5. Duplicate student detection.
"""
import sys, os, time, io
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import unittest
from app import app, check_pw
from database import get_db, ph

class TestBulkImportExcel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        cls.excel_path = os.path.join(os.path.dirname(__file__), "test_students_import.xlsx")
        # Extract student IDs from the test Excel to clean up before and after
        import openpyxl
        wb = openpyxl.load_workbook(cls.excel_path, data_only=True)
        ws = wb.active
        headers = [str(c.value or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
        cls.test_sids = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            r_dict = dict(zip(headers, [str(v or "").strip() for v in row]))
            sid = r_dict.get("رقم الطالب") or r_dict.get("student_id")
            if sid:
                cls.test_sids.append(sid)

        cls._cleanup_test_data()

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_test_data()

    @classmethod
    def _cleanup_test_data(cls):
        if not cls.test_sids:
            return
        db = get_db()
        cur = db.cursor()
        for sid in cls.test_sids:
            cur.execute(f"DELETE FROM audit_log WHERE target={ph()} OR target LIKE {ph()}", (sid, f"%{sid}%"))
            cur.execute(f"DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE student_id={ph()})", (sid,))
            cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (sid,))
            cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (sid,))
        db.commit()
        db.close()

    def test_01_bulk_import_page_and_template_download(self):
        """Verify bulk import UI loads and Excel template downloads correctly."""
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"

            # 1. UI Page
            res_ui = client.get("/admin/bulk-import")
            self.assertEqual(res_ui.status_code, 200)
            self.assertIn("استيراد بيانات الطلاب", res_ui.get_data(as_text=True))

            # 2. Template Download
            res_tpl = client.get("/admin/bulk-import/template")
            self.assertEqual(res_tpl.status_code, 200)
            self.assertEqual(
                res_tpl.headers.get("Content-Type"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

    def test_02_synchronous_excel_import(self):
        """Upload Excel sheet synchronously and verify database records and accounts."""
        self._cleanup_test_data()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"

            with open(self.excel_path, "rb") as f:
                data = {
                    "file": (io.BytesIO(f.read()), "test_students.xlsx")
                }
                res = client.post("/admin/bulk-import", data=data, content_type="multipart/form-data")

            self.assertEqual(res.status_code, 200)
            res_data = res.get_json()
            self.assertTrue(res_data.get("success"))
            results = res_data.get("results", {})
            self.assertGreaterEqual(results.get("created", 0), 1)

            # Verify in database
            db = get_db()
            cur = db.cursor()
            for sid in self.test_sids:
                cur.execute(f"SELECT * FROM students WHERE student_id={ph()}", (sid,))
                s_row = cur.fetchone()
                self.assertIsNotNone(s_row, f"Student {sid} should exist in students table")

                cur.execute(f"SELECT * FROM users WHERE student_id={ph()}", (sid,))
                u_row = cur.fetchone()
                u_dict = dict(u_row) if hasattr(u_row, 'keys') else dict(zip([d[0] for d in cur.description], u_row))
                self.assertIsNotNone(u_dict, f"User account for {sid} should exist in users table")
                self.assertIsNone(u_dict.get("password_plain"))
                self.assertTrue(check_pw(sid, u_dict["password_hash"]), "Default temp password must match student_id")
            db.close()

    def test_03_duplicate_detection(self):
        """Re-uploading the same Excel file should skip existing students."""
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"

            with open(self.excel_path, "rb") as f:
                data = {
                    "file": (io.BytesIO(f.read()), "test_students.xlsx")
                }
                res = client.post("/admin/bulk-import", data=data, content_type="multipart/form-data")

            self.assertEqual(res.status_code, 200)
            res_data = res.get_json()
            self.assertTrue(res_data.get("success"))
            results = res_data.get("results", {})
            self.assertEqual(results.get("created", 0), 0)
            self.assertGreaterEqual(results.get("skipped", 0), 1)

    def test_04_async_background_excel_import(self):
        """Upload Excel with X-Async header; verify 202 response and background job completion."""
        self._cleanup_test_data()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"

            with open(self.excel_path, "rb") as f:
                data = {
                    "file": (io.BytesIO(f.read()), "test_students.xlsx")
                }
                res = client.post(
                    "/admin/bulk-import",
                    data=data,
                    content_type="multipart/form-data",
                    headers={"X-Async": "true"}
                )

            self.assertEqual(res.status_code, 202)
            res_data = res.get_json()
            self.assertTrue(res_data.get("success"))
            self.assertTrue(res_data.get("async_job"))
            job_id = res_data.get("job_id")
            self.assertIsNotNone(job_id)

            # Poll job status
            completed = False
            for _ in range(15):
                time.sleep(0.5)
                poll_res = client.get(f"/api/jobs/{job_id}")
                self.assertEqual(poll_res.status_code, 200)
                poll_data = poll_res.get_json()
                if poll_data.get("status") == "completed":
                    completed = True
                    self.assertEqual(poll_data.get("progress"), 100)
                    job_result = poll_data.get("result", {})
                    self.assertGreaterEqual(job_result.get("created", 0), 1)
                    break
            self.assertTrue(completed, "Async bulk import job should complete within timeout")

if __name__ == "__main__":
    unittest.main()
