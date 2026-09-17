"""
tests/test_background_jobs.py – Test suite for Background Job Queue & Async Image Processing
"""
import io
import os
import sys
import time
import unittest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ["TESTING"] = "1"
os.environ["STORAGE_DRIVER"] = "local"

from app import app, get_db, ph
from background_worker import (
    submit_photo_processing_job, get_job_status, wait_for_job,
    submit_async_email, JobStatus, MAX_IMAGE_WORKERS
)


class TestBackgroundJobs(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()

        # Create valid mock test image with a face
        img = Image.new("RGB", (600, 800), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.ellipse([200, 150, 400, 420], fill=(220, 180, 150))
        draw.ellipse([250, 240, 280, 265], fill=(50, 50, 50))
        draw.ellipse([320, 240, 350, 265], fill=(50, 50, 50))
        draw.polygon([(300, 275), (290, 315), (310, 315)], fill=(180, 130, 100))
        draw.rectangle([270, 340, 330, 355], fill=(170, 70, 70))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        self.valid_jpg = buf.getvalue()

        # Insert test student
        self.student_id = "20269999"
        db = get_db()
        cur = db.cursor()
        cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (self.student_id,))
        cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (self.student_id,))
        cur.execute(
            f"""INSERT INTO students (student_id, full_name, year, college, email, image_path)
                VALUES ({','.join([ph()]*6)})""",
            (self.student_id, "طالب مهام خلفية", "2026", "كلية الصيدلة", "bg@bua.edu.eg", "uploads/2026/كلية_الصيدلة/dummy.jpg")
        )
        cur.execute(
            f"""INSERT INTO users (email, password_hash, full_name, role, college, student_id, is_active, email_verified)
                VALUES ({','.join([ph()]*8)})""",
            ("bg@bua.edu.eg", "hash", "طالب مهام خلفية", "student", "كلية الصيدلة", self.student_id, True, True)
        )
        db.commit()
        db.close()

    def tearDown(self):
        db = get_db()
        cur = db.cursor()
        cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (self.student_id,))
        cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (self.student_id,))
        cur.execute(f"DELETE FROM background_jobs WHERE target_id={ph()}", (self.student_id,))
        db.commit()
        db.close()

        saved_file = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "2026", "كلية_الصيدلة", f"{self.student_id}.jpg")
        if os.path.exists(saved_file):
            try:
                os.remove(saved_file)
            except Exception:
                pass

        self.app_context.pop()

    def test_01_submit_photo_job_lifecycle(self):
        """Verify photo processing job is enqueued, processed async, and reaches COMPLETED."""
        upload_folder = os.path.join(os.path.dirname(__file__), "..", "static", "uploads")
        static_root = os.path.join(os.path.dirname(__file__), "..", "static")

        job_id = submit_photo_processing_job(
            raw_bytes=self.valid_jpg,
            student_id=self.student_id,
            year="2026",
            college="كلية الصيدلة",
            upload_folder=upload_folder,
            static_root=static_root,
            auto_crop=True,
            db_update=True
        )

        self.assertIsNotNone(job_id)
        # Initial status should be pending or processing
        status_info = get_job_status(job_id)
        self.assertIn(status_info["status"], (JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.COMPLETED))

        # Wait for completion
        job = wait_for_job(job_id, timeout=8.0)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], JobStatus.COMPLETED)
        self.assertIn("result", job)
        self.assertIn("url", job["result"])
        self.assertEqual(job["progress"], 100)

        # Check DB updated
        db = get_db()
        cur = db.cursor()
        cur.execute(f"SELECT image_path FROM students WHERE student_id={ph()}", (self.student_id,))
        row = cur.fetchone()
        db.close()
        self.assertIsNotNone(row)
        img_path = row["image_path"] if isinstance(row, dict) else row[0]
        self.assertTrue(img_path.startswith("uploads/2026/كلية_الصيدلة/"))

        print("\n[SUCCESS] Test 1: Photo background job completed with 100% progress and DB updated!")

    def test_02_job_status_api(self):
        """Verify /api/jobs/<job_id> returns proper JSON status and progress."""
        upload_folder = os.path.join(os.path.dirname(__file__), "..", "static", "uploads")
        static_root = os.path.join(os.path.dirname(__file__), "..", "static")

        job_id = submit_photo_processing_job(
            raw_bytes=self.valid_jpg,
            student_id=self.student_id,
            year="2026",
            college="كلية الصيدلة",
            upload_folder=upload_folder,
            static_root=static_root
        )

        # Poll the API endpoint
        res = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["job_id"], job_id)
        self.assertIn(data["status"], ("pending", "processing", "completed"))

        # Wait to finish
        wait_for_job(job_id, timeout=6.0)
        res2 = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()
        self.assertEqual(data2["status"], "completed")
        self.assertEqual(data2["progress"], 100)
        self.assertIsNotNone(data2["result"])

        print("\n[SUCCESS] Test 2: /api/jobs/<job_id> endpoint works with real-time status!")

    def test_03_concurrent_photo_jobs(self):
        """Verify bounded worker pool processes multiple jobs in parallel without lock or drops."""
        upload_folder = os.path.join(os.path.dirname(__file__), "..", "static", "uploads")
        static_root = os.path.join(os.path.dirname(__file__), "..", "static")

        num_jobs = 5
        job_ids = []

        start_time = time.time()
        for i in range(num_jobs):
            jid = submit_photo_processing_job(
                raw_bytes=self.valid_jpg,
                student_id=f"{self.student_id}_{i}",
                year="2026",
                college="كلية الصيدلة",
                upload_folder=upload_folder,
                static_root=static_root,
                db_update=False
            )
            job_ids.append(jid)

        enqueue_time = time.time() - start_time
        # All 5 jobs should be enqueued virtually instantly (e.g. < 50ms)
        self.assertLess(enqueue_time, 0.5, "Submitting jobs took too long; queue is blocking!")

        # Wait for all jobs to complete
        for jid in job_ids:
            job = wait_for_job(jid, timeout=10.0)
            self.assertEqual(job["status"], JobStatus.COMPLETED)

        print(f"\n[SUCCESS] Test 3: {num_jobs} concurrent photo jobs queued in {enqueue_time*1000:.1f}ms and completed cleanly!")


if __name__ == "__main__":
    unittest.main()
