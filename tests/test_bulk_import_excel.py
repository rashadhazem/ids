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
        import openpyxl
        if not os.path.exists(cls.excel_path):
            wb = openpyxl.Workbook()
            ws = wb.active
            headers = ["رقم الطالب", "الاسم الكامل", "السنة", "الكلية", "البريد الإلكتروني"]
            ws.append(headers)
            sample_data = [
                ["2026101001", "على محمد على أحمد", "2026", "كلية الحاسبات والمعلومات", "ali.2026101001@bua.edu.eg"],
                ["2026101002", "سارة محمود إبراهيم حسن", "2026", "كلية الطب البشري", "sara.2026101002@bua.edu.eg"],
                ["2026101003", "عمر خالد يوسف النجار", "2026", "كلية الهندسة", "omar.2026101003@bua.edu.eg"],
                ["2026101004", "نورهان طارق مصطفى محمود", "2026", "كلية طب الأسنان", "nourhan.2026101004@bua.edu.eg"],
                ["2026101005", "أحمد حسن عبد الرحمن خليل", "2026", "كلية الصيدلة فارما D", "ahmed.2026101005@bua.edu.eg"],
                ["2026101006", "مريم ياسر كمال الشريف", "2026", "كلية العلاج الطبيعي", "mariam.2026101006@bua.edu.eg"],
                ["2026101007", "زياد سامح فؤاد إبراهيم", "2026", "كلية إدارة الأعمال", "ziad.2026101007@bua.edu.eg"],
                ["2026101008", "آية هاني فتحي السيد", "2026", "كلية تكنولوجيا العلوم الصحية", "aya.2026101008@bua.edu.eg"],
            ]
            for row in sample_data:
                ws.append(row)
            wb.save(cls.excel_path)

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
                self.assertFalse(check_pw(sid, u_dict["password_hash"]), "Security fix (S3): Temp password must NOT be predictable student_id")
                self.assertTrue(bool(u_dict.get("password_hash")), "User must have a valid bcrypt password hash")
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

    def test_05_import_with_title_banner_and_no_email_and_floats(self):
        """
        Verify Excel file with:
        - Row 1: University title banner
        - Row 2: Headers (using 'كود الطالب', 'اسم الطالب', 'الفرقة الدراسية', 'الكلية')
        - Missing email column completely
        - Float student ID (2026202001.0) and float year (2026.0)
        - Trailing blank rows
        """
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["جامعة بدر بأسيوط - كشف أسماء الطلاب المقيدين"])
        ws.append(["كود الطالب", "اسم الطالب", "الفرقة الدراسية", "الكلية"])
        ws.append([2026202001.0, "حسام مصطفى كمال الدين", 2026.0, "ذكاء اصطناعي"])
        ws.append([2026202002, "ياسمين عادل إبراهيم مرسي", "2026", "كلية الصيدلة فارما D"])
        ws.append([None, None, None, None])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        sids = ["2026202001", "2026202002"]
        db = get_db()
        cur = db.cursor()
        for sid in sids:
            cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (sid,))
            cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (sid,))
        db.commit()
        db.close()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"

            data = {"file": (buf, "real_world_test.xlsx")}
            res = client.post("/admin/bulk-import", data=data, content_type="multipart/form-data")

            self.assertEqual(res.status_code, 200)
            res_data = res.get_json()
            self.assertTrue(res_data.get("success"))
            results = res_data.get("results", {})
            self.assertEqual(results.get("created"), 2)
            self.assertEqual(len(results.get("errors")), 0)

            # Verify in DB
            db = get_db()
            cur = db.cursor()
            for sid in sids:
                cur.execute(f"SELECT * FROM students WHERE student_id={ph()}", (sid,))
                s_row = cur.fetchone()
                self.assertIsNotNone(s_row)

                cur.execute(f"SELECT * FROM users WHERE student_id={ph()}", (sid,))
                u_row = cur.fetchone()
                self.assertIsNotNone(u_row)
                u_dict = dict(u_row) if hasattr(u_row, 'keys') else dict(zip([d[0] for d in cur.description], u_row))
                self.assertTrue(u_dict["email"].startswith(sid))
                self.assertEqual(u_dict["role"], "student")

                # Clean up
                cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (sid,))
                cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (sid,))
            db.commit()
            db.close()

    def test_06_import_with_email_named_al_email_and_al_bareed_al_elektrony(self):
        """
        Verify Excel files with email column headers named:
        1. 'الايميل'
        2. 'البريد الالكترونى'
        Both should be detected correctly and personal email saved in students table.
        """
        import openpyxl

        # Test Sheet 1: Header is 'الايميل'
        wb1 = openpyxl.Workbook()
        ws1 = wb1.active
        ws1.append(["رقم الطالب", "اسم الطالب", "الكلية", "الايميل"])
        ws1.append(["2026303001", "كريم حسام الدين عبد الله", "كلية طب الأسنان", "karim@example.com"])
        buf1 = io.BytesIO()
        wb1.save(buf1)
        buf1.seek(0)

        # Test Sheet 2: Header is 'البريد الالكترونى'
        wb2 = openpyxl.Workbook()
        ws2 = wb2.active
        ws2.append(["كود الطالب", "الاسم", "الكلية", "البريد الالكترونى"])
        ws2.append(["2026303002", "منة الله طارق السيد", "كلية التمريض", "menna@example.com"])
        buf2 = io.BytesIO()
        wb2.save(buf2)
        buf2.seek(0)

        sids = ["2026303001", "2026303002"]
        db = get_db()
        cur = db.cursor()
        for sid in sids:
            cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (sid,))
            cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (sid,))
        db.commit()
        db.close()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"

            # 1. Upload sheet with 'الايميل'
            res1 = client.post("/admin/bulk-import", data={"file": (buf1, "sheet1.xlsx")}, content_type="multipart/form-data")
            self.assertEqual(res1.status_code, 200)
            self.assertTrue(res1.get_json().get("success"))
            self.assertEqual(res1.get_json().get("results", {}).get("created"), 1)

            # 2. Upload sheet with 'البريد الالكترونى'
            res2 = client.post("/admin/bulk-import", data={"file": (buf2, "sheet2.xlsx")}, content_type="multipart/form-data")
            self.assertEqual(res2.status_code, 200)
            self.assertTrue(res2.get_json().get("success"))
            self.assertEqual(res2.get_json().get("results", {}).get("created"), 1)

        # Verify emails were correctly extracted and saved
        db = get_db()
        cur = db.cursor()

        cur.execute(f"SELECT email FROM students WHERE student_id={ph()}", ("2026303001",))
        s1 = cur.fetchone()
        s1_dict = dict(s1) if hasattr(s1, 'keys') else {"email": s1[0]}
        self.assertEqual(s1_dict["email"], "karim@example.com")

        cur.execute(f"SELECT email FROM students WHERE student_id={ph()}", ("2026303002",))
        s2 = cur.fetchone()
        s2_dict = dict(s2) if hasattr(s2, 'keys') else {"email": s2[0]}
        self.assertEqual(s2_dict["email"], "menna@example.com")

        # Clean up
        for sid in sids:
            cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (sid,))
            cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (sid,))
        db.commit()
        db.close()

    def test_07_import_actual_rashad_xlsx(self):
        """
        Verify the actual user-provided sample file rashad.xlsx:
        - Contains column header 'الإيمبيل'
        - Contains Row 8 with email 'mohamed.2020123456.bua.edu.eg' (auto-fixed to @)
        - Contains 8 students
        - Verifies clean import and duplicate detection
        """
        rashad_file = os.path.join(os.path.dirname(__file__), "..", "rashad.xlsx")
        self.assertTrue(os.path.exists(rashad_file), "rashad.xlsx must exist in project root")

        import openpyxl
        wb = openpyxl.load_workbook(rashad_file, data_only=True)
        ws = wb.active
        sids = []
        for r in list(ws.iter_rows(values_only=True))[1:]:
            if r and r[0]:
                sids.append(str(r[0]).strip())

        # Clean DB for fresh import test
        db = get_db()
        cur = db.cursor()
        for sid in sids:
            cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (sid,))
            cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (sid,))
        db.commit()
        db.close()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["role"] = "superadmin"
                sess["user_name"] = "مدير النظام"

            with open(rashad_file, "rb") as f:
                res = client.post("/admin/bulk-import", data={"file": (f, "rashad.xlsx")}, content_type="multipart/form-data")

            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get("success"))
            results = data.get("results", {})
            self.assertEqual(results.get("created"), 8)
            self.assertEqual(len(results.get("errors")), 0)

            # Re-upload should detect all 8 as duplicates
            with open(rashad_file, "rb") as f:
                res_dup = client.post("/admin/bulk-import", data={"file": (f, "rashad.xlsx")}, content_type="multipart/form-data")
            self.assertEqual(res_dup.status_code, 200)
            self.assertEqual(res_dup.get_json().get("results", {}).get("skipped"), 8)
            self.assertEqual(res_dup.get_json().get("results", {}).get("created"), 0)

        # Verify emails extracted properly from 'الإيمبيل'
        db = get_db()
        cur = db.cursor()
        cur.execute(f"SELECT email FROM students WHERE student_id={ph()}", ("2023056972",))
        row1 = cur.fetchone()
        row1_dict = dict(row1) if hasattr(row1, "keys") else {"email": row1[0]}
        self.assertEqual(row1_dict["email"], "abdulrahman.2023056972@bua.edu.eg")

        cur.execute(f"SELECT email FROM students WHERE student_id={ph()}", ("2020123456",))
        row8 = cur.fetchone()
        row8_dict = dict(row8) if hasattr(row8, "keys") else {"email": row8[0]}
        self.assertEqual(row8_dict["email"], "mohamed.2020123456@bua.edu.eg")
        db.close()

if __name__ == "__main__":
    unittest.main()
