import os
import io
import re
import shutil
from datetime import datetime
import numpy as np
import cv2
from PIL import Image, ImageOps
from dotenv import load_dotenv

load_dotenv()

try:
    from gdrive_helper import upload_to_gdrive, archive_in_gdrive, move_student_in_gdrive, is_gdrive_configured
except ImportError:
    upload_to_gdrive = lambda *a, **kw: False
    archive_in_gdrive = lambda *a, **kw: False
    move_student_in_gdrive = lambda *a, **kw: False
    is_gdrive_configured = lambda: False

TARGET_W = 400
TARGET_H = 500
JPEG_Q = 88  # output quality

# OpenCV face detection cascades
_CASCADES = []
try:
    _CASCADE_PATHS = [
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
        cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml",
        cv2.data.haarcascades + "haarcascade_frontalface_alt.xml",
    ]
    if hasattr(cv2, "CascadeClassifier"):
        for path in _CASCADE_PATHS:
            if os.path.exists(path):
                cascade = cv2.CascadeClassifier(path)
                if not cascade.empty():
                    _CASCADES.append(cascade)
    else:
        print("OpenCV cv2 module does not expose CascadeClassifier; skipping cascade face detection.")
except ImportError:
    pass


def _fix_exif_rotation(pil_img: Image.Image) -> Image.Image:
    """Auto-rotate image based on EXIF orientation tag."""
    try:
        return ImageOps.exif_transpose(pil_img)
    except Exception:
        return pil_img


def _bytes_to_cv(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _cv_to_bytes(img: np.ndarray, quality: int = JPEG_Q) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return bytes(buf) if ok else b""


def _pil_to_bytes(img: Image.Image, quality: int = JPEG_Q) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _normalize_image_bytes(image_bytes: bytes) -> bytes:
    """
    Normalize image bytes so detection and cropping work on the same orientation.
    """
    pil = Image.open(io.BytesIO(image_bytes))
    pil = _fix_exif_rotation(pil).convert("RGB")
    return _pil_to_bytes(pil)


def _non_max_suppression(boxes: list[tuple[int, int, int, int]], overlap_thresh: float = 0.3) -> list[tuple[int, int, int, int]]:
    """
    Apply Non-Maximum Suppression to eliminate overlapping bounding boxes for the same face.
    boxes format: [(x, y, w, h), ...]
    """
    if not boxes:
        return []

    rects = []
    for (x, y, w, h) in boxes:
        rects.append([x, y, x + w, y + h, w * h])

    rects = sorted(rects, key=lambda b: b[4], reverse=True)
    picked = []

    while rects:
        current = rects.pop(0)
        picked.append((current[0], current[1], current[2] - current[0], current[3] - current[1]))

        remaining = []
        for r in rects:
            xx1 = max(current[0], r[0])
            yy1 = max(current[1], r[1])
            xx2 = min(current[2], r[2])
            yy2 = min(current[3], r[3])

            w_inter = max(0, xx2 - xx1)
            h_inter = max(0, yy2 - yy1)
            inter_area = w_inter * h_inter

            min_area = min(current[4], r[4])
            iou = inter_area / float(current[4] + r[4] - inter_area) if (current[4] + r[4] - inter_area) > 0 else 0
            overlap_smaller = inter_area / float(min_area) if min_area > 0 else 0

            if iou < overlap_thresh and overlap_smaller < 0.5:
                remaining.append(r)
        rects = remaining

    return picked


def detect_faces_opencv(image_bytes: bytes) -> list[tuple[int, int, int, int]]:
    """Fast, optimized face detection using OpenCV cascades with CLAHE and early-exit."""
    if not _CASCADES:
        return []
    
    cv_img = _bytes_to_cv(image_bytes)
    if cv_img is None:
        return []

    orig_h, orig_w = cv_img.shape[:2]
    max_dim = 640  # Downscale to 640px for 4x faster detection
    scale_x, scale_y = 1.0, 1.0
    if max(orig_h, orig_w) > max_dim:
        sc = max_dim / max(orig_h, orig_w)
        cv_img = cv2.resize(cv_img, (int(orig_w * sc), int(orig_h * sc)), interpolation=cv2.INTER_AREA)
        det_h, det_w = cv_img.shape[:2]
        scale_x = orig_w / det_w
        scale_y = orig_h / det_h

    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    all_detected = []
    for idx, cascade in enumerate(_CASCADES):
        detected = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(28, 28)
        )
        if len(detected) > 0:
            scaled = [(int(x * scale_x), int(y * scale_y), int(w * scale_x), int(h * scale_y))
                      for (x, y, w, h) in detected]
            all_detected.extend(scaled)
            # If the primary cascade found a clear face, early exit to save CPU
            if idx == 0 and len(detected) == 1:
                break

    return _non_max_suppression(all_detected)


def detect_faces(image_bytes: bytes) -> list[tuple[int, int, int, int]]:
    """
    Return list of (x, y, w, h) face rectangles detected via OpenCV frontal face cascades.
    """
    normalized_bytes = _normalize_image_bytes(image_bytes)
    return detect_faces_opencv(normalized_bytes)


def count_faces(image_bytes: bytes) -> int:
    """Return total number of distinct human faces detected in the image."""
    return len(detect_faces(image_bytes))


def validate_single_person(image_bytes: bytes) -> tuple[bool, str, list[tuple[int, int, int, int]]]:
    """
    Validate that the uploaded image contains strictly ONE person/face.
    Returns (is_valid: bool, message: str, faces: list).
    """
    try:
        faces = detect_faces(image_bytes)
        count = len(faces)
        if count == 0:
            return (
                False,
                "لم يتم اكتشاف أي وجه بشري في الصورة. يرجى رفع صورة شخصية واضحة تظهر الوجه بالكامل.",
                [],
            )
        elif count > 1:
            return (
                False,
                f"تحتوي الصورة على أكثر من شخص ({count} أشخاص). يجب أن تحتوي الصورة على شخص واحد فقط.",
                faces,
            )
        return True, "تم التحقق من الصورة بنجاح (شخص واحد).", faces
    except Exception as e:
        return False, f"حدث خطأ أثناء معالجة فحص الصورة: {e}", []


def face_detected(image_bytes: bytes) -> bool:
    """Check if at least one face is detected."""
    return len(detect_faces(image_bytes)) > 0


def smart_crop_face(image_bytes: bytes, faces: list = None) -> bytes:
    """
    Auto-crop the image centered on the detected face with strict 4:5 aspect ratio (400x500).
    Guarantees that the entire face (hair, forehead, eyes, nose, chin, neck) is completely
    visible and fills the ID card frame 100% with zero empty gaps or distortion.
    """
    normalized_bytes = _normalize_image_bytes(image_bytes)
    pil = Image.open(io.BytesIO(normalized_bytes)).convert("RGB")
    iw, ih = pil.size

    if faces is None:
        faces = detect_faces(normalized_bytes)
    target_ar = TARGET_W / TARGET_H  # 400 / 500 = 0.8

    if faces:
        # Choose the most prominent face
        fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])

        # Anatomical estimates from Haar face box:
        # Haar cascade rectangle spans from eyebrows/eyes to chin
        # Top of skull / hair is ~35% of fh above fy
        # Chin / jawline is ~12% of fh below fy + fh
        head_top = max(0, fy - int(fh * 0.35))
        chin_bottom = min(ih, fy + fh + int(fh * 0.12))
        head_height = max(1, chin_bottom - head_top)
        face_cx = fx + fw // 2
        eye_y = fy + int(fh * 0.35)

        # ID / Passport standards: head should occupy ~58-62% of photo height
        desired_crop_h = int(head_height / 0.60)
        desired_crop_w = int(desired_crop_h * target_ar)

        # If desired crop exceeds image boundaries, fit largest 4:5 box possible
        if desired_crop_h > ih or desired_crop_w > iw:
            if iw / ih > target_ar:
                crop_h = ih
                crop_w = int(crop_h * target_ar)
            else:
                crop_w = iw
                crop_h = int(crop_w / target_ar)
        else:
            crop_h = desired_crop_h
            crop_w = desired_crop_w

        # Ensure crop is at least big enough to contain the face if possible
        if crop_h < int(head_height * 1.25) and int(head_height * 1.25) <= ih and int(head_height * 1.25 * target_ar) <= iw:
            crop_h = int(head_height * 1.25)
            crop_w = int(crop_h * target_ar)

        # Horizontal positioning: centered on face midline
        left = int(face_cx - crop_w // 2)
        left = max(0, min(left, iw - crop_w))

        # Vertical positioning: eyes positioned around 38-40% from top
        target_top = eye_y - int(crop_h * 0.38)

        # Ensure headroom above head_top
        if target_top > head_top - int(crop_h * 0.08):
            target_top = head_top - int(crop_h * 0.08)

        # Ensure chin is well above bottom
        if target_top + crop_h < chin_bottom + int(crop_h * 0.06):
            target_top = chin_bottom + int(crop_h * 0.06) - crop_h

        # Clamp within image bounds
        top = max(0, min(target_top, ih - crop_h))

    else:
        # Fallback portrait-aware 4:5 crop without detected face
        if iw / ih > target_ar:
            crop_h = ih
            crop_w = int(ih * target_ar)
            left = (iw - crop_w) // 2
            top = 0
        else:
            crop_w = iw
            crop_h = int(iw / target_ar)
            left = 0
            # Position towards upper-third where heads typically reside
            top = max(0, min(int((ih - crop_h) * 0.20), ih - crop_h))

    # Crop and high-quality resize to exact target dimensions
    cropped = pil.crop((left, top, left + crop_w, top + crop_h))
    resized = cropped.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    return _pil_to_bytes(resized)


def apply_edits(
    image_bytes: bytes,
    rotation: int = 0,
    flip_h: bool = False,
    zoom: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    auto_crop: bool = True,
    faces: list = None,
) -> bytes:
    """
    Apply manual edits from the front-end canvas editor, then
    optionally run smart face-crop + resize.
    """
    pil = Image.open(io.BytesIO(image_bytes))
    pil = _fix_exif_rotation(pil).convert("RGB")

    if flip_h:
        pil = pil.transpose(Image.FLIP_LEFT_RIGHT)

    if rotation:
        pil = pil.rotate(-rotation, expand=True)

    if zoom != 1.0 or offset_x != 0.0 or offset_y != 0.0:
        iw, ih = pil.size
        new_w = max(1, int(iw / zoom))
        new_h = max(1, int(ih / zoom))
        cx = iw // 2 + int(offset_x * iw * 0.5)
        cy = ih // 2 + int(offset_y * ih * 0.5)
        left = max(0, cx - new_w // 2)
        top = max(0, cy - new_h // 2)
        left = min(left, max(0, iw - new_w))
        top = min(top, max(0, ih - new_h))
        pil = pil.crop((left, top, left + new_w, top + new_h))

    edited_bytes = _pil_to_bytes(pil)

    if auto_crop:
        return smart_crop_face(edited_bytes, faces=faces)

    # Ensure 4:5 aspect ratio without distortion if manual crop
    target_ar = TARGET_W / TARGET_H
    iw, ih = pil.size
    if iw / ih > target_ar:
        crop_w = int(ih * target_ar)
        crop_h = ih
        left = (iw - crop_w) // 2
        top = 0
    else:
        crop_h = int(iw / target_ar)
        crop_w = iw
        left = 0
        top = max(0, min(int((ih - crop_h) * 0.20), ih - crop_h))
    cropped = pil.crop((left, top, left + crop_w, top + crop_h))
    resized = cropped.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    return _pil_to_bytes(resized)


def process_and_validate_photo(
    raw_bytes: bytes,
    rotation: int = 0,
    flip_h: bool = False,
    zoom: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    auto_crop: bool = True,
) -> tuple[bool, str, bytes | None]:
    """
    High-performance single-pass image processor and face validator.
    Applies user canvas adjustments, detects face ONCE, verifies strictly 1 person,
    and returns (is_valid, msg, processed_bytes).
    """
    try:
        # 1. Apply user edits
        pil = Image.open(io.BytesIO(raw_bytes))
        pil = _fix_exif_rotation(pil).convert("RGB")
        if flip_h:
            pil = pil.transpose(Image.FLIP_LEFT_RIGHT)
        if rotation:
            pil = pil.rotate(-rotation, expand=True)

        iw, ih = pil.size
        if zoom != 1.0 or offset_x != 0.0 or offset_y != 0.0:
            new_w = max(1, int(iw / zoom))
            new_h = max(1, int(ih / zoom))
            cx = iw // 2 + int(offset_x * iw * 0.5)
            cy = ih // 2 + int(offset_y * ih * 0.5)
            left = max(0, min(cx - new_w // 2, max(0, iw - new_w)))
            top = max(0, min(cy - new_h // 2, max(0, ih - new_h)))
            pil = pil.crop((left, top, left + new_w, top + new_h))

        edited_bytes = _pil_to_bytes(pil)

        # 2. Single-pass face detection
        faces = detect_faces(edited_bytes)
        count = len(faces)
        if count == 0:
            return False, "لم يتم اكتشاف أي وجه بشري في الصورة. يرجى رفع صورة شخصية واضحة تظهر الوجه بالكامل.", None
        if count > 1:
            return False, f"تحتوي الصورة على أكثر من شخص ({count} أشخاص). يجب أن تحتوي الصورة على شخص واحد فقط.", None

        # 3. Smart crop using already-detected face (0 redundant cascade runs!)
        if auto_crop:
            final_bytes = smart_crop_face(edited_bytes, faces=faces)
        else:
            target_ar = TARGET_W / TARGET_H
            iw, ih = pil.size
            if iw / ih > target_ar:
                crop_w = int(ih * target_ar)
                crop_h = ih
                left = (iw - crop_w) // 2
                top = 0
            else:
                crop_h = int(iw / target_ar)
                crop_w = iw
                left = 0
                top = max(0, min(int((ih - crop_h) * 0.20), ih - crop_h))
            cropped = pil.crop((left, top, left + crop_w, top + crop_h))
            final_bytes = _pil_to_bytes(cropped.resize((TARGET_W, TARGET_H), Image.LANCZOS))

        return True, "تم التحقق من الصورة بنجاح (شخص واحد).", final_bytes

    except Exception as e:
        return False, f"خطأ أثناء معالجة الصورة: {e}", None


def _college_folder(college: str) -> str:
    """Return a safe folder name for the college (Arabic-friendly, spaces→underscore)."""
    safe = re.sub(r'[\s/\\:*?"<>|]+', "_", college.strip())
    return safe.rstrip("_") or "عام"


def save_image(
    image_bytes: bytes, student_id: str, year: str, college: str, upload_root: str, skip_validation: bool = True
) -> dict:
    """
    Save processed image directly to local VPS storage.
    Path: uploads/{year}/{college}/{student_id}.jpg
    Returns { "path": relative_path, "url": public_url }
    """
    if not skip_validation:
        is_valid, msg, _ = validate_single_person(image_bytes)
        if not is_valid:
            raise ValueError(msg)

    year_folder = os.path.join(upload_root, year)
    os.makedirs(year_folder, exist_ok=True)

    col_folder = _college_folder(college)
    college_folder = os.path.join(year_folder, col_folder)
    os.makedirs(college_folder, exist_ok=True)

    filename = f"{student_id}.jpg"
    full_path = os.path.join(college_folder, filename)

    with open(full_path, "wb") as f:
        f.write(image_bytes)

    # Optional background sync to Google Drive (never blocks student upload on VPS)
    if os.getenv("ENABLE_GDRIVE_SYNC", "false").lower() == "true":
        try:
            if is_gdrive_configured():
                import threading
                threading.Thread(
                    target=upload_to_gdrive,
                    args=(image_bytes, year, college, filename),
                    daemon=True
                ).start()
        except Exception as e:
            print(f"[GDrive Async] Upload dispatch error: {e}")

    rel_path = os.path.relpath(full_path, os.path.join(upload_root, "..")).replace(
        "\\", "/"
    )
    return {"path": rel_path, "url": f"/static/{rel_path}"}


def _extract_year_and_college(path: str) -> tuple[str, str] | tuple[None, None]:
    parts = path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if re.match(r"^\d{4}$", part):
            if i + 1 < len(parts):
                return part, parts[i+1]
    return None, None


def archive_old_image(
    old_rel_path: str, student_id: str, static_root: str, upload_root: str
) -> str | None:
    """
    Move old image to old/ subdirectory.
    Also archives the old photo on Google Drive.
    Only keeps one old photo ({student_id}_old.jpg) in the old folder.
    Returns new relative path or None if file not found.
    """
    if not old_rel_path:
        return None

    old_full_path = os.path.join(static_root, old_rel_path)
    if not os.path.exists(old_full_path):
        return None

    year, college = _extract_year_and_college(old_rel_path)
    old_filename = f"{student_id}_old.jpg"

    # Mirror archive to Google Drive (keeps only one old photo per student)
    try:
        if is_gdrive_configured() and year and college:
            archive_in_gdrive(student_id, year, college, old_filename)
    except Exception as e:
        print(f"[GDrive] Failed to archive old image: {e}")

    # Create old/ subdirectory in the same local directory as the image
    image_dir = os.path.dirname(old_full_path)
    old_dir = os.path.join(image_dir, "old")
    os.makedirs(old_dir, exist_ok=True)

    new_full_path = os.path.join(old_dir, old_filename)

    try:
        # Clean up any legacy or existing old photos for this student in local old/ folder
        for fname in os.listdir(old_dir):
            if fname.startswith(f"{student_id}_old"):
                legacy_file = os.path.join(old_dir, fname)
                try:
                    os.remove(legacy_file)
                except Exception:
                    pass

        shutil.move(old_full_path, new_full_path)
        rel_path = os.path.relpath(new_full_path, static_root).replace("\\", "/")
        return rel_path
    except Exception as e:
        print(f"Error archiving image: {e}")
        return None


def move_student_images_locally(
    old_rel_path: str,
    old_student_id: str,
    old_year: str,
    old_college: str,
    new_student_id: str,
    new_year: str,
    new_college: str,
    static_root: str,
    upload_root: str,
) -> str | None:
    """
    Move a student's active image and any old archived image from:
      uploads/{old_year}/{old_college}/{old_student_id}.jpg
    to:
      uploads/{new_year}/{new_college}/{new_student_id}.jpg
    Also moves/mirrors the move in Google Drive.
    Returns the new relative path (e.g. 'uploads/2023/كلية_الصيدلة/2023006972.jpg') or None.
    """
    # 1. First trigger Google Drive move if configured
    try:
        if is_gdrive_configured() and old_year and old_college and new_year and new_college:
            move_student_in_gdrive(
                str(old_student_id),
                str(old_year),
                str(old_college),
                str(new_student_id),
                str(new_year),
                str(new_college),
            )
    except Exception as e:
        print(f"[GDrive] Failed to move student in Google Drive: {e}")

    # 2. Target paths locally
    new_col_folder = _college_folder(new_college)
    new_dir = os.path.join(upload_root, str(new_year), new_col_folder)
    os.makedirs(new_dir, exist_ok=True)
    new_file_path = os.path.join(new_dir, f"{new_student_id}.jpg")
    new_rel_path = os.path.relpath(new_file_path, static_root).replace("\\", "/")

    # 3. Locate old file
    old_full_path = os.path.join(static_root, old_rel_path) if old_rel_path else None
    old_col_folder = _college_folder(old_college) if old_college else ""
    fallback_old = (
        os.path.join(upload_root, str(old_year), old_col_folder, f"{old_student_id}.jpg")
        if old_year and old_col_folder
        else None
    )

    src_file = None
    if old_full_path and os.path.exists(old_full_path):
        src_file = old_full_path
    elif fallback_old and os.path.exists(fallback_old):
        src_file = fallback_old

    if src_file:
        try:
            if os.path.abspath(src_file) != os.path.abspath(new_file_path):
                # If target already exists, remove it first
                if os.path.exists(new_file_path):
                    try:
                        os.remove(new_file_path)
                    except Exception:
                        pass
                shutil.move(src_file, new_file_path)
                try:
                    print(f"[Storage] Moved student image to {new_file_path}")
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"Error moving student active image locally: {e}")
            except Exception:
                pass

        # 4. Check for and move archived old photo in old/
        src_dir = os.path.dirname(src_file)
        old_archive_dir = os.path.join(src_dir, "old")
        if os.path.exists(old_archive_dir):
            old_archive_file = os.path.join(old_archive_dir, f"{old_student_id}_old.jpg")
            if os.path.exists(old_archive_file):
                new_archive_dir = os.path.join(new_dir, "old")
                os.makedirs(new_archive_dir, exist_ok=True)
                new_archive_file = os.path.join(new_archive_dir, f"{new_student_id}_old.jpg")
                try:
                    if os.path.exists(new_archive_file):
                        try:
                            os.remove(new_archive_file)
                        except Exception:
                            pass
                    shutil.move(old_archive_file, new_archive_file)
                    try:
                        print(f"[Storage] Moved student archive image to {new_archive_file}")
                    except Exception:
                        pass
                except Exception as e:
                    try:
                        print(f"Error moving student archived image locally: {e}")
                    except Exception:
                        pass

    return new_rel_path