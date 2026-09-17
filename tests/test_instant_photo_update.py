import io
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
from PIL import Image, ImageDraw

# Set testing environment
os.environ["TESTING"] = "1"
os.environ["STORAGE_DRIVER"] = "local"

from app import app, get_db, ph, _row_to_dict
from image_processor import TARGET_W, TARGET_H

class TestInstantPhotoUpdate(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()

        # Create a mock test image with a face-like structure
        img = Image.new("RGB", (600, 800), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        # Head oval
        draw.ellipse([200, 150, 400, 420], fill=(220, 180, 150))
        # Eyes
        draw.ellipse([250, 240, 280, 265], fill=(50, 50, 50))
        draw.ellipse([320, 240, 350, 265], fill=(50, 50, 50))
        # Nose
        draw.polygon([(300, 275), (290, 315), (310, 315)], fill=(180, 130, 100))
        # Mouth
        draw.rectangle([270, 340, 330, 355], fill=(170, 70, 70))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        self.valid_jpg = buf.getvalue()

        # Insert test student and user
        self.student_id = "20268888"
        db = get_db()
        cur = db.cursor()
        cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (self.student_id,))
        cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (self.student_id,))
        cur.execute(
            f"""INSERT INTO students (student_id, full_name, year, college, email, image_path)
                VALUES ({','.join([ph()]*6)})""",
            (self.student_id, "طالب تحديث فوري", "2026", "كلية الصيدلة", "instant@bua.edu.eg", "uploads/2026/كلية_الصيدلة/dummy.jpg")
        )
        cur.execute(
            f"""INSERT INTO users (email, password_hash, full_name, role, college, student_id, is_active, email_verified)
                VALUES ({','.join([ph()]*8)})""",
            ("instant@bua.edu.eg", "hash", "طالب تحديث فوري", "student", "كلية الصيدلة", self.student_id, True, True)
        )
        db.commit()
        db.close()

    def tearDown(self):
        db = get_db()
        cur = db.cursor()
        cur.execute(f"DELETE FROM students WHERE student_id={ph()}", (self.student_id,))
        cur.execute(f"DELETE FROM users WHERE student_id={ph()}", (self.student_id,))
        db.commit()
        db.close()

        # Clean up test photo file from disk if created
        test_file = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "2026", "كلية_الصيدلة", f"{self.student_id}.jpg")
        if os.path.exists(test_file):
            try:
                os.remove(test_file)
            except Exception:
                pass
        self.app_context.pop()

    def test_instant_photo_update_api(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = 9999
            sess["student_id"] = self.student_id
            sess["role"] = "student"
            sess["user_name"] = "طالب تحديث فوري"

        # Fetch student card to get CSRF token
        res_page = self.client.get(f"/student/{self.student_id}")
        self.assertEqual(res_page.status_code, 200)

        # Extract CSRF token
        import re
        m = re.search(r'name="csrf-token" content="([^"]+)"', res_page.data.decode('utf-8'))
        csrf_token = m.group(1) if m else ""

        # Post update photo
        data = {
            "image": (io.BytesIO(self.valid_jpg), "new_photo.jpg"),
            "auto_crop": "1",
            "csrf_token": csrf_token
        }
        res = self.client.post(
            f"/student/{self.student_id}/update-photo",
            data=data,
            content_type="multipart/form-data",
            headers={"X-CSRFToken": csrf_token}
        )

        self.assertEqual(res.status_code, 200)
        json_data = res.get_json()
        self.assertTrue(json_data["success"])
        self.assertIn("url", json_data)
        self.assertIn("new_url", json_data)
        self.assertEqual(json_data["url"], json_data["new_url"])
        self.assertTrue(json_data["url"].startswith("/static/uploads/"))

        # Verify disk file exists and has TARGET_W x TARGET_H
        saved_rel = json_data["path"]
        saved_full = os.path.join(app.config.get("UPLOAD_FOLDER", "static/uploads"), "..", saved_rel)
        saved_full = os.path.normpath(saved_full)
        self.assertTrue(os.path.exists(saved_full), f"Saved file not found: {saved_full}")

        with Image.open(saved_full) as saved_img:
            self.assertEqual(saved_img.size, (TARGET_W, TARGET_H))

        print("\n[SUCCESS] Instant photo update test passed! Response has both url and new_url, and file is cropped to 400x500.")

if __name__ == "__main__":
    unittest.main()
