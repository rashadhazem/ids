"""
background_worker.py – Asynchronous Background Job Engine & Worker Pool
Provides non-blocking, queued processing for:
  • Heavy OpenCV / YuNet face detection, smart cropping & image optimization
  • Google Drive uploads and archiving with controlled retries
  • Asynchronous bulk student imports with real-time progress tracking
  • Non-blocking email dispatches
"""

import os
import io
import json
import time
import uuid
import logging
import threading
import secrets
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Dict, Optional

from database import get_db, ph, is_use_pg

logger = logging.getLogger("background_worker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [BGWorker] %(message)s"))
    logger.addHandler(ch)

# ── Concurrency Configuration ───────────────────────────────────────────────
# Bounded worker pool ensures CPU is not starved by hundreds of concurrent OpenCV operations
_CPU_COUNT = os.cpu_count() or 4
MAX_IMAGE_WORKERS = int(os.getenv("MAX_IMAGE_WORKERS", min(8, max(2, _CPU_COUNT))))
MAX_GENERAL_WORKERS = int(os.getenv("MAX_GENERAL_WORKERS", min(16, max(4, _CPU_COUNT * 2))))

# Dedicated thread pool executors
_image_pool = ThreadPoolExecutor(max_workers=MAX_IMAGE_WORKERS, thread_name_prefix="img_proc")
_general_pool = ThreadPoolExecutor(max_workers=MAX_GENERAL_WORKERS, thread_name_prefix="bg_task")

# In-memory fast-lookup cache for job status with thread-safe lock
_jobs_lock = threading.RLock()
_jobs_cache: Dict[str, Dict[str, Any]] = {}
_jobs_futures: Dict[str, Future] = {}


class JobStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Database Persistence Helpers ───────────────────────────────────────────

def _persist_job_created(job_id: str, job_type: str, target_id: str = None, payload: dict = None):
    try:
        db = get_db()
        cur = db.cursor()
        payload_str = json.dumps(payload or {}, ensure_ascii=False)
        cur.execute(
            f"""INSERT INTO background_jobs (id, job_type, status, progress, target_id, payload_json, created_at)
                VALUES ({','.join([ph()]*7)})""",
            (job_id, job_type, JobStatus.PENDING, 0, target_id, payload_str, datetime.now().isoformat())
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"Failed to persist job creation ({job_id}): {e}")


def _persist_job_status(job_id: str, status: str, progress: int = None, result: dict = None, error: str = None):
    now_iso = datetime.now().isoformat()
    try:
        db = get_db()
        cur = db.cursor()
        updates = [f"status={ph()}"]
        vals = [status]

        if progress is not None:
            updates.append(f"progress={ph()}")
            vals.append(progress)

        if status == JobStatus.PROCESSING:
            updates.append(f"started_at={ph()}")
            vals.append(now_iso)

        if status in (JobStatus.COMPLETED, JobStatus.FAILED):
            updates.append(f"completed_at={ph()}")
            vals.append(now_iso)

        if result is not None:
            updates.append(f"result_json={ph()}")
            vals.append(json.dumps(result, ensure_ascii=False))

        if error is not None:
            updates.append(f"error_message={ph()}")
            vals.append(str(error)[:1000])

        vals.append(job_id)
        sql = f"UPDATE background_jobs SET {', '.join(updates)} WHERE id={ph()}"
        cur.execute(sql, tuple(vals))
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"Failed to update job status in DB ({job_id}): {e}")


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve job metadata, status, progress, and result from cache or DB."""
    with _jobs_lock:
        if job_id in _jobs_cache:
            return dict(_jobs_cache[job_id])

    # Fallback to DB
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(f"SELECT * FROM background_jobs WHERE id={ph()}", (job_id,))
        row = cur.fetchone()
        db.close()
        if not row:
            return None

        # Convert row to dict
        if hasattr(row, "keys"):
            data = dict(row)
        else:
            cols = [d[0] for d in cur.description]
            data = dict(zip(cols, row))

        # Parse JSON fields
        if data.get("payload_json"):
            try:
                data["payload"] = json.loads(data["payload_json"])
            except Exception:
                data["payload"] = {}
        if data.get("result_json"):
            try:
                data["result"] = json.loads(data["result_json"])
            except Exception:
                data["result"] = {}

        with _jobs_lock:
            _jobs_cache[job_id] = data
        return data
    except Exception as e:
        logger.error(f"Error reading job status for {job_id}: {e}")
        return None


# ── Job Submissions & Handlers ─────────────────────────────────────────────

def submit_photo_processing_job(
    raw_bytes: bytes,
    student_id: str,
    year: str,
    college: str,
    upload_folder: str,
    static_root: str,
    rotation: int = 0,
    flip_h: bool = False,
    zoom: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    auto_crop: bool = True,
    archive_old_rel: Optional[str] = None,
    db_update: bool = True,
    user_id: Optional[int] = None
) -> str:
    """
    Enqueue an image processing job into the dedicated Image Worker Pool.
    Returns immediately with a unique job_id.
    """
    job_id = str(uuid.uuid4())
    initial_meta = {
        "id": job_id,
        "job_type": "process_photo",
        "status": JobStatus.PENDING,
        "progress": 0,
        "target_id": student_id,
        "created_at": datetime.now().isoformat(),
        "result": None,
        "error": None
    }

    with _jobs_lock:
        _jobs_cache[job_id] = initial_meta

    _persist_job_created(job_id, "process_photo", target_id=student_id, payload={
        "student_id": student_id, "year": year, "college": college, "auto_crop": auto_crop
    })

    def _worker_task():
        with _jobs_lock:
            _jobs_cache[job_id]["status"] = JobStatus.PROCESSING
            _jobs_cache[job_id]["progress"] = 15
        _persist_job_status(job_id, JobStatus.PROCESSING, progress=15)

        try:
            from image_processor import process_and_validate_photo, save_image, archive_old_image

            # 1. Image processing and face detection (CPU intensive)
            ok, face_msg, processed = process_and_validate_photo(
                raw_bytes,
                rotation=rotation,
                flip_h=flip_h,
                zoom=zoom,
                offset_x=offset_x,
                offset_y=offset_y,
                auto_crop=auto_crop
            )

            if not ok:
                err_msg = face_msg or "لم يتم التحقق من وجود وجه واضح في الصورة"
                with _jobs_lock:
                    _jobs_cache[job_id]["status"] = JobStatus.FAILED
                    _jobs_cache[job_id]["error"] = err_msg
                _persist_job_status(job_id, JobStatus.FAILED, error=err_msg)
                return {"success": False, "message": err_msg}

            with _jobs_lock:
                _jobs_cache[job_id]["progress"] = 55
            _persist_job_status(job_id, JobStatus.PROCESSING, progress=55)

            # 2. Optional archiving of old photo
            archived_path = None
            if archive_old_rel:
                try:
                    archived_path = archive_old_image(archive_old_rel, student_id, static_root, upload_folder)
                except Exception as ex:
                    logger.warning(f"Failed to archive old image for {student_id}: {ex}")

            with _jobs_lock:
                _jobs_cache[job_id]["progress"] = 75
            _persist_job_status(job_id, JobStatus.PROCESSING, progress=75)

            # 3. Save processed image directly to local storage
            result = save_image(processed, student_id, year, college, upload_folder, skip_validation=True)

            # 4. Update Database record if requested
            if db_update:
                try:
                    db = get_db()
                    cur = db.cursor()
                    cur.execute(
                        f"UPDATE students SET image_path={ph()}, updated_at={ph()} WHERE student_id={ph()}",
                        (result["path"], datetime.now().isoformat(), student_id)
                    )
                    db.commit()
                    db.close()
                except Exception as dbe:
                    logger.error(f"Failed to update students table for {student_id}: {dbe}")

            result_data = {
                "success": True,
                "url": result["url"],
                "new_url": result["url"],
                "path": result["path"],
                "student_id": student_id,
                "archived_path": archived_path
            }

            # 5. Persist job completion to DB first, so DB transactions are committed and closed
            _persist_job_status(job_id, JobStatus.COMPLETED, progress=100, result=result_data)

            # 6. Update in-memory cache and signal completion
            with _jobs_lock:
                _jobs_cache[job_id]["status"] = JobStatus.COMPLETED
                _jobs_cache[job_id]["progress"] = 100
                _jobs_cache[job_id]["result"] = result_data
            logger.info(f"Photo job completed successfully for {student_id} (job {job_id})")
            return result_data

        except Exception as e:
            logger.exception(f"Exception during photo processing job {job_id}: {e}")
            err = f"حدث خطأ أثناء معالجة الصورة: {str(e)}"
            _persist_job_status(job_id, JobStatus.FAILED, error=err)
            with _jobs_lock:
                _jobs_cache[job_id]["status"] = JobStatus.FAILED
                _jobs_cache[job_id]["error"] = err
            return {"success": False, "message": err}

    future = _image_pool.submit(_worker_task)
    with _jobs_lock:
        _jobs_futures[job_id] = future

    return job_id


def wait_for_job(job_id: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """
    Wait up to `timeout` seconds for a job to finish.
    If it finishes within the timeout, returns the job dict with status.
    If timeout expires, returns the current status (processing / pending).
    """
    with _jobs_lock:
        fut = _jobs_futures.get(job_id)

    if fut:
        try:
            fut.result(timeout=timeout)
        except Exception:
            pass

    return get_job_status(job_id)


# ── Asynchronous Email Task ────────────────────────────────────────────────

_SMTP_QUOTA_BLOCKED_UNTIL = 0

def submit_async_email(flask_app, to: str, subject: str, html: str) -> str:
    """Submit an email to be sent asynchronously in the background."""
    job_id = str(uuid.uuid4())

    def _send_task():
        global _SMTP_QUOTA_BLOCKED_UNTIL
        with flask_app.app_context():
            from flask_mail import Message
            from app import mail
            import re

            if flask_app.config.get("TESTING"):
                logger.info(f"📧 [TESTING] Simulated async email to {to}: {subject}")
                return

            if not flask_app.config.get("MAIL_USERNAME") or not flask_app.config.get("MAIL_PASSWORD"):
                logger.warning(f"MAIL not configured – skipping email to {to}")
                return

            now = time.time()
            if now < _SMTP_QUOTA_BLOCKED_UNTIL:
                # Quota is currently known to be exhausted on SMTP server
                logger.warning(f"⚠️ [SMTP Quota Cooldown] Gmail daily quota currently reached. Direct delivery paused.")
                links = re.findall(r'href=[\'"](http[^\'"]+)[\'"]', html)
                if links:
                    logger.info(f"🔗 [Action Link for {to}]: {links[0]}")
                return

            for attempt in range(1, 3):
                try:
                    msg = Message(subject, recipients=[to], html=html)
                    mail.send(msg)
                    logger.info(f"📧 Async email sent successfully to {to}")
                    return
                except Exception as e:
                    ctx = getattr(e, '__context__', None)
                    err_str = f"{e} {ctx}" if ctx else str(e)
                    is_quota = ("550" in err_str and "limit" in err_str.lower()) or "sending limit exceeded" in err_str.lower()
                    
                    if is_quota or "Connection unexpectedly closed" in str(e):
                        # Gmail abruptly drops socket on daily limit exceeded
                        _SMTP_QUOTA_BLOCKED_UNTIL = time.time() + 1800  # 30-min cooldown
                        logger.warning(f"⚠️ [SMTP Quota Alert] Google SMTP daily user sending limit reached (550). Recipient: {to}")
                        links = re.findall(r'href=[\'"](http[^\'"]+)[\'"]', html)
                        if links:
                            logger.info(f"🔗 [Action Link for {to}]: {links[0]}")
                        return
                    else:
                        logger.error(f"Attempt {attempt} failed sending email to {to}: {e}")
                        time.sleep(1.0)

    _general_pool.submit(_send_task)
    return job_id


# ── Asynchronous Google Drive Sync Task ─────────────────────────────────────

def submit_async_gdrive_sync(image_bytes: bytes, year: str, college: str, filename: str) -> str:
    """Enqueue Google Drive upload in the background with retry."""
    job_id = str(uuid.uuid4())

    def _gdrive_task():
        try:
            from gdrive_helper import upload_to_gdrive, is_gdrive_configured
            if is_gdrive_configured():
                upload_to_gdrive(image_bytes, year, college, filename)
                logger.info(f"Async GDrive upload complete: {year}/{college}/{filename}")
        except Exception as e:
            logger.error(f"Async GDrive upload failed for {filename}: {e}")

    _general_pool.submit(_gdrive_task)
    return job_id


def submit_async_gdrive_archive(student_id: str, year: str, college: str, old_filename: str) -> str:
    """Enqueue Google Drive archive operation in the background."""
    job_id = str(uuid.uuid4())

    def _archive_task():
        try:
            from gdrive_helper import archive_in_gdrive, is_gdrive_configured
            if is_gdrive_configured():
                archive_in_gdrive(student_id, year, college, old_filename)
                logger.info(f"Async GDrive archive complete for student {student_id}")
        except Exception as e:
            logger.error(f"Async GDrive archive failed for student {student_id}: {e}")

    _general_pool.submit(_archive_task)
    return job_id


# ── Asynchronous Bulk Import Task ──────────────────────────────────────────

def submit_bulk_import_job(flask_app, rows: list, user_id: int, user_role: str, user_college: str) -> str:
    """
    Process up to hundreds/thousands of student records in the background,
    updating progress continuously and dispatching emails asynchronously.
    """
    job_id = str(uuid.uuid4())
    initial_meta = {
        "id": job_id,
        "job_type": "bulk_import",
        "status": JobStatus.PENDING,
        "progress": 0,
        "target_id": f"bulk_{len(rows)}",
        "created_at": datetime.now().isoformat(),
        "result": None,
        "error": None
    }

    with _jobs_lock:
        _jobs_cache[job_id] = initial_meta
    _persist_job_created(job_id, "bulk_import", target_id=f"bulk_{len(rows)}", payload={"total_rows": len(rows)})

    def _bulk_task():
        try:
            with _jobs_lock:
                _jobs_cache[job_id]["status"] = JobStatus.PROCESSING
            _persist_job_status(job_id, JobStatus.PROCESSING, progress=0)
            logger.info(f"Starting _bulk_task for job {job_id} with {len(rows)} rows")

            with flask_app.app_context():
                from app import (
                    COLLEGES, UNIVERSITY_DOMAIN, CURRENT_YEAR, UPLOAD_FOLDER,
                    hash_pw, _send_student_welcome
                )
                from bulk_import_helper import (
                    find_col, COL_MAP, to_eng,
                    match_college_name, extract_academic_year
                )
                from image_processor import _college_folder

                results = {"created": 0, "skipped": 0, "errors": [], "preview": []}
                total = len(rows)

                placeholder_img = os.path.join(UPLOAD_FOLDER, "placeholder.jpg")
                if not os.path.exists(placeholder_img):
                    try:
                        from PIL import Image, ImageDraw
                        img_ph = Image.new("RGB", (400, 500), color=(26, 58, 107))
                        draw   = ImageDraw.Draw(img_ph)
                        draw.rectangle([160, 100, 240, 180], fill=(232, 184, 75))
                        img_ph.save(placeholder_img, "JPEG")
                    except Exception:
                        pass

                db = get_db()
                cur = db.cursor()
                try:
                    for idx, row in enumerate(rows, start=1):
                        sid       = to_eng(find_col(row, COL_MAP["student_id"]).strip())
                        full_name = find_col(row, COL_MAP["full_name"]).strip()
                        raw_year  = find_col(row, COL_MAP["year"]).strip()
                        year      = extract_academic_year(raw_year, sid, CURRENT_YEAR)
                        raw_coll  = find_col(row, COL_MAP["college"]).strip()
                        college   = match_college_name(raw_coll, user_role=user_role, user_college=user_college)
                        email     = find_col(row, COL_MAP["email"]).strip().lower()

                        # Only sid and full_name are required. Email is optional!
                        if not sid or not full_name:
                            results["errors"].append(f"سطر {idx+1}: رقم الطالب أو الاسم مفقود")
                            results["skipped"] += 1
                            continue

                        try:
                            cur.execute(f"SELECT id FROM students WHERE student_id={ph()}", (sid,))
                            if cur.fetchone():
                                results["skipped"] += 1
                                results["preview"].append({"sid": sid, "name": full_name, "status": "موجود مسبقاً"})
                                continue

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

                            reg_user_id = None
                            if user_id:
                                cur.execute(f"SELECT id FROM users WHERE id={ph()}", (user_id,))
                                if cur.fetchone():
                                    reg_user_id = user_id

                            cur.execute(
                                f"INSERT INTO students (student_id,full_name,year,college,email,image_path,registered_by) VALUES ({','.join([ph()]*7)})",
                                (sid, full_name, year, college, email or None, rel_path, reg_user_id)
                            )
                            db.commit()

                            student_login_email = f"{sid}@{UNIVERSITY_DOMAIN}"
                            temp_pw             = secrets.token_urlsafe(8)
                            hashed_pw           = hash_pw(temp_pw)

                            cur.execute(f"SELECT id FROM users WHERE email={ph()} OR student_id={ph()}", (student_login_email, sid))
                            existing_user = cur.fetchone()
                            if not existing_user:
                                cur.execute(
                                    f"INSERT INTO users (email,password_hash,full_name,role,college,student_id,is_active,email_verified) VALUES ({','.join([ph()]*8)})",
                                    (student_login_email, hashed_pw, full_name, "student",
                                     college, sid, True if is_use_pg() else 1, True if is_use_pg() else 1)
                                )
                                db.commit()
                            else:
                                user_id_val = existing_user["id"] if isinstance(existing_user, dict) else existing_user[0]
                                cur.execute(
                                    f"UPDATE users SET student_id={ph()}, full_name={ph()}, college={ph()}, is_active={ph()}, email_verified={ph()} WHERE id={ph()}",
                                    (sid, full_name, college, True if is_use_pg() else 1, True if is_use_pg() else 1, user_id_val)
                                )
                                db.commit()

                            results["created"] += 1
                            results["preview"].append({"sid": sid, "name": full_name, "status": "تم الإنشاء ✓"})

                            # Send welcome email only if valid personal email provided
                            if email and "@" in email and "." in email:
                                card_link = f"{flask_app.config.get('SITE_URL', 'http://localhost:5000')}/student/{sid}"
                                _send_student_welcome(email, full_name, sid, student_login_email, temp_pw, card_link)

                        except Exception as row_err:
                            results["errors"].append(f"سطر {idx+1} ({sid}): {str(row_err)}")
                            results["skipped"] += 1

                        # Update progress
                        progress_pct = int((idx / total) * 100)
                        with _jobs_lock:
                            _jobs_cache[job_id]["progress"] = progress_pct
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass

                with _jobs_lock:
                    _jobs_cache[job_id]["status"] = JobStatus.COMPLETED
                    _jobs_cache[job_id]["progress"] = 100
                    _jobs_cache[job_id]["result"] = results

                _persist_job_status(job_id, JobStatus.COMPLETED, progress=100, result=results)
                logger.info(f"Bulk import job {job_id} finished: {results['created']} created, {results['skipped']} skipped.")
                return results

        except Exception as e:
            logger.exception(f"Exception during bulk import job {job_id}: {e}")
            err = f"حدث خطأ أثناء استيراد البيانات: {str(e)}"
            _persist_job_status(job_id, JobStatus.FAILED, error=err)
            with _jobs_lock:
                _jobs_cache[job_id]["status"] = JobStatus.FAILED
                _jobs_cache[job_id]["error"] = err
            return {"created": 0, "skipped": len(rows), "errors": [str(e)]}

    future = _general_pool.submit(_bulk_task)
    with _jobs_lock:
        _jobs_futures[job_id] = future
    return job_id

