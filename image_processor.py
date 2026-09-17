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

try:
    from s3_helper import (
        upload_image_to_s3,
        archive_image_in_s3,
        move_student_in_s3,
        is_s3_configured,
        delete_image_from_s3,
        get_image_bytes,
    )
except ImportError:
    is_s3_configured = lambda: False
    upload_image_to_s3 = lambda *a, **kw: None
    archive_image_in_s3 = lambda *a, **kw: None
    move_student_in_s3 = lambda *a, **kw: None
    delete_image_from_s3 = lambda *a, **kw: False
    get_image_bytes = lambda *a, **kw: None

TARGET_W = 400
TARGET_H = 500
JPEG_Q = 88  # output quality

# OpenCV face detection cascades & Deep Learning YuNet
_CASCADES = []
try:
    _CASCADE_PATHS = [
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
        cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml",
        cv2.data.haarcascades + "haarcascade_frontalface_alt.xml",
        cv2.data.haarcascades + "haarcascade_profileface.xml",
    ]
    if hasattr(cv2, "CascadeClassifier"):
        for path in _CASCADE_PATHS:
            if os.path.exists(path):
                cascade = cv2.CascadeClassifier(path)
                if not cascade.empty():
                    _CASCADES.append(cascade)
except Exception:
    pass

_YUNET_DETECTOR = None
_YUNET_MODEL_NAME = "face_detection_yunet_2023mar.onnx"

def _get_yunet_detector():
    """Load or initialize OpenCV YuNet deep learning face detector."""
    global _YUNET_DETECTOR
    if _YUNET_DETECTOR is not None:
        return _YUNET_DETECTOR

    candidate_paths = [
        os.path.join(os.path.dirname(__file__), "models", _YUNET_MODEL_NAME),
        os.path.join(os.path.dirname(__file__), _YUNET_MODEL_NAME),
        os.path.join(os.getcwd(), "models", _YUNET_MODEL_NAME),
        os.path.join(os.getcwd(), _YUNET_MODEL_NAME),
    ]
    model_path = None
    for p in candidate_paths:
        if os.path.exists(p) and os.path.getsize(p) > 100000:
            model_path = p
            break

    if not model_path:
        return None

    try:
        if hasattr(cv2, "FaceDetectorYN"):
            _YUNET_DETECTOR = cv2.FaceDetectorYN.create(
                model_path, "", (320, 320),
                score_threshold=0.5,
                nms_threshold=0.3,
                top_k=5000
            )
    except Exception as e:
        print(f"Warning: Failed to create YuNet detector from {model_path}: {e}")
        _YUNET_DETECTOR = None

    return _YUNET_DETECTOR


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


def detect_faces_detailed(image_bytes: bytes) -> list[dict]:
    """
    Detect human faces with high accuracy.
    Uses YuNet DNN as primary detector (accurate across angles, lighting, distances).
    Falls back to multi-scale Haar Cascades with CLAHE.
    Returns list of dicts:
    [{'box': (x, y, w, h), 'eyes': (eye_cx, eye_cy), 'score': float}, ...]
    """
    normalized_bytes = _normalize_image_bytes(image_bytes)
    cv_img = _bytes_to_cv(normalized_bytes)
    if cv_img is None:
        return []

    orig_h, orig_w = cv_img.shape[:2]

    # 1. Try YuNet deep learning face detector first
    yunet = _get_yunet_detector()
    if yunet is not None:
        try:
            max_dim = 1280
            scale = 1.0
            det_img = cv_img
            if max(orig_h, orig_w) > max_dim:
                scale = max_dim / float(max(orig_h, orig_w))
                det_img = cv2.resize(cv_img, (int(orig_w * scale), int(orig_h * scale)), interpolation=cv2.INTER_AREA)

            det_h, det_w = det_img.shape[:2]
            yunet.setInputSize((det_w, det_h))
            _, faces = yunet.detect(det_img)

            if faces is not None and len(faces) > 0:
                results = []
                inv_scale = 1.0 / scale
                for f in faces:
                    score = float(f[-1])
                    if score < 0.40:
                        continue
                    fx = int(f[0] * inv_scale)
                    fy = int(f[1] * inv_scale)
                    fw = int(f[2] * inv_scale)
                    fh = int(f[3] * inv_scale)

                    fx = max(0, min(fx, orig_w - 1))
                    fy = max(0, min(fy, orig_h - 1))
                    fw = max(1, min(fw, orig_w - fx))
                    fh = max(1, min(fh, orig_h - fy))

                    re_x, re_y = f[4] * inv_scale, f[5] * inv_scale
                    le_x, le_y = f[6] * inv_scale, f[7] * inv_scale
                    eye_cx = (re_x + le_x) / 2.0
                    eye_cy = (re_y + le_y) / 2.0

                    results.append({
                        "box": (fx, fy, fw, fh),
                        "eyes": (eye_cx, eye_cy),
                        "score": score,
                    })

                if results:
                    return results
        except Exception as e:
            print(f"Warning: YuNet detection failed, falling back to Haar: {e}")

    # 2. Fallback to OpenCV Haar cascades with CLAHE enhancement
    if not _CASCADES:
        return []

    max_dim = 1000
    scale_x, scale_y = 1.0, 1.0
    det_img = cv_img
    if max(orig_h, orig_w) > max_dim:
        sc = max_dim / float(max(orig_h, orig_w))
        det_img = cv2.resize(cv_img, (int(orig_w * sc), int(orig_h * sc)), interpolation=cv2.INTER_AREA)
        det_h, det_w = det_img.shape[:2]
        scale_x = orig_w / float(det_w)
        scale_y = orig_h / float(det_h)

    gray = cv2.cvtColor(det_img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    all_detected = []
    for idx, cascade in enumerate(_CASCADES):
        detected = cascade.detectMultiScale(
            gray, scaleFactor=1.08, minNeighbors=4, minSize=(20, 20)
        )
        if len(detected) > 0:
            scaled = [(int(x * scale_x), int(y * scale_y), int(w * scale_x), int(h * scale_y))
                      for (x, y, w, h) in detected]
            all_detected.extend(scaled)
            if idx == 0 and len(detected) == 1:
                break

    nms_boxes = _non_max_suppression(all_detected)
    return [
        {
            "box": b,
            "eyes": (b[0] + b[2] / 2.0, b[1] + b[3] * 0.35),
            "score": 0.8,
        }
        for b in nms_boxes
    ]


def detect_faces(image_bytes: bytes) -> list[tuple[int, int, int, int]]:
    """
    Return list of (x, y, w, h) face rectangles. Backward-compatible.
    """
    detailed = detect_faces_detailed(image_bytes)
    return [f["box"] for f in detailed]


def count_faces(image_bytes: bytes) -> int:
    """Return total number of distinct human faces detected in the image."""
    return len(detect_faces(image_bytes))


def validate_single_person(image_bytes: bytes) -> tuple[bool, str, list]:
    """
    Validate that the uploaded image contains strictly ONE person/face.
    Returns (is_valid: bool, message: str, faces: list).
    """
    try:
        faces = detect_faces_detailed(image_bytes)
        count = len(faces)
        if count == 0:
            return (
                False,
                "لم يتم اكتشاف أي وجه بشري واضح في الصورة. يرجى رفع صورة شخصية واضحة تركز على الوجه.",
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
    Auto-crop image into a professional ID card / passport portrait:
    - Focuses strictly on the person's head, face, and collar (bust).
    - Eliminates the lower body, torso, legs, feet, and wide surrounding background.
    - Preserves entire head, hair, headwear, chin, and neck without clipping.
    - Guarantees exact 4:5 aspect ratio (400x500 pixels) with 100% frame fill,
      zero black letterbox bars, and zero distortion.
    """
    normalized_bytes = _normalize_image_bytes(image_bytes)
    pil = Image.open(io.BytesIO(normalized_bytes)).convert("RGB")
    iw, ih = pil.size

    target_ar = TARGET_W / TARGET_H  # 400 / 500 = 0.8

    # If faces not provided, get detailed face detections
    if faces is None:
        faces = detect_faces_detailed(normalized_bytes)
    elif faces and isinstance(faces[0], (list, tuple)):
        # Convert legacy (x, y, w, h) tuples to detailed dicts
        faces = [
            {
                "box": f,
                "eyes": (f[0] + f[2] / 2.0, f[1] + f[3] * 0.35),
                "score": 0.9,
            }
            for f in faces
        ]

    if faces:
        # Select the most prominent face
        best = max(faces, key=lambda r: r["box"][2] * r["box"][3])
        fx, fy, fw, fh = best["box"]
        eye_cx, eye_cy = best.get("eyes", (fx + fw / 2.0, fy + fh * 0.35))

        # Anatomical boundaries of head:
        # Top of skull / hair / hijab is ~35% of fh above fy
        # Chin / jawline is ~10% of fh below fy + fh
        head_top = max(0, fy - int(fh * 0.35))
        chin_bottom = min(ih, fy + fh + int(fh * 0.10))
        head_height = max(1, chin_bottom - head_top)

        # Professional ID / Passport standard:
        # Head height occupies ~68% of frame height (strict headshot focus).
        # This completely discards torso, arms, legs, and background clutter.
        desired_crop_h = int(head_height / 0.68)
        desired_crop_w = int(desired_crop_h * target_ar)

        # If desired crop exceeds image boundaries (extreme close-up), fit max 4:5 box
        if desired_crop_h > ih or desired_crop_w > iw:
            if iw / float(ih) > target_ar:
                crop_h = ih
                crop_w = int(crop_h * target_ar)
            else:
                crop_w = iw
                crop_h = int(crop_w / target_ar)
        else:
            crop_h = desired_crop_h
            crop_w = desired_crop_w

        # Ensure crop is at least big enough to contain the full head
        min_crop_h = int(head_height * 1.25)
        if crop_h < min_crop_h and min_crop_h <= ih and int(min_crop_h * target_ar) <= iw:
            crop_h = min_crop_h
            crop_w = int(crop_h * target_ar)

        # Horizontal alignment: centered precisely on eye midpoint / face midline
        face_mid_x = int(eye_cx)
        left = face_mid_x - crop_w // 2
        left = max(0, min(left, iw - crop_w))

        # Vertical alignment: eye line placed at standard 38% from top of crop
        target_top = int(eye_cy - crop_h * 0.38)

        # Headroom safety: guarantee at least 8% headroom above top of hair
        headroom_limit = head_top - int(crop_h * 0.08)
        if target_top > headroom_limit:
            target_top = headroom_limit

        # Chin clearance: guarantee chin is above bottom by at least 6%
        chin_limit = chin_bottom + int(crop_h * 0.06) - crop_h
        if target_top < chin_limit:
            target_top = chin_limit

        # Clamp strictly within image bounds
        top = max(0, min(target_top, ih - crop_h))

    else:
        # Fallback when no face detected: focus on upper portrait portion (bust/head area)
        # to NEVER show legs or full-body clutter
        if iw / float(ih) > target_ar:
            crop_h = ih
            crop_w = int(ih * target_ar)
            left = (iw - crop_w) // 2
            top = 0
        else:
            crop_h = min(ih, int(ih * 0.45))
            crop_w = int(crop_h * target_ar)
            if crop_w > iw:
                crop_w = iw
                crop_h = int(crop_w / target_ar)
            left = (iw - crop_w) // 2
            top = max(0, min(int(ih * 0.05), ih - crop_h))

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

    has_manual_pan_zoom = (zoom != 1.0 or offset_x != 0.0 or offset_y != 0.0)
    if has_manual_pan_zoom:
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

    # Always auto-crop to face unless user explicitly panned/zoomed manually
    if auto_crop or not has_manual_pan_zoom:
        return smart_crop_face(edited_bytes, faces=faces)

    # Ensure 4:5 aspect ratio without distortion if manual crop
    target_ar = TARGET_W / TARGET_H
    iw, ih = pil.size
    if iw / float(ih) > target_ar:
        crop_w = int(ih * target_ar)
        crop_h = ih
        left = (iw - crop_w) // 2
        top = 0
    else:
        crop_h = min(ih, int(iw / target_ar))
        crop_w = int(crop_h * target_ar)
        left = (iw - crop_w) // 2
        top = max(0, min(int((ih - crop_h) * 0.15), ih - crop_h))
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

        has_manual_pan_zoom = (zoom != 1.0 or offset_x != 0.0 or offset_y != 0.0)
        if has_manual_pan_zoom:
            iw, ih = pil.size
            new_w = max(1, int(iw / zoom))
            new_h = max(1, int(ih / zoom))
            cx = iw // 2 + int(offset_x * iw * 0.5)
            cy = ih // 2 + int(offset_y * ih * 0.5)
            left = max(0, min(cx - new_w // 2, max(0, iw - new_w)))
            top = max(0, min(cy - new_h // 2, max(0, ih - new_h)))
            pil = pil.crop((left, top, left + new_w, top + new_h))

        edited_bytes = _pil_to_bytes(pil)

        # 2. Single-pass face detection using deep YuNet + multi-scale cascades
        faces = detect_faces_detailed(edited_bytes)
        count = len(faces)
        if count == 0:
            return False, "لم يتم اكتشاف أي وجه بشري واضح في الصورة. يرجى رفع صورة شخصية واضحة تركز على الوجه.", None
        if count > 1:
            return False, f"تحتوي الصورة على أكثر من شخص ({count} أشخاص). يجب أن تحتوي الصورة على شخص واحد فقط.", None

        # 3. Smart crop to face: eliminates full body, legs, and surroundings
        if auto_crop or not has_manual_pan_zoom:
            final_bytes = smart_crop_face(edited_bytes, faces=faces)
        else:
            target_ar = TARGET_W / TARGET_H
            iw, ih = pil.size
            if iw / float(ih) > target_ar:
                crop_w = int(ih * target_ar)
                crop_h = ih
                left = (iw - crop_w) // 2
                top = 0
            else:
                crop_h = min(ih, int(iw / target_ar))
                crop_w = int(crop_h * target_ar)
                left = (iw - crop_w) // 2
                top = max(0, min(int((ih - crop_h) * 0.15), ih - crop_h))
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

    s3_result = None
    if is_s3_configured():
        try:
            s3_result = upload_image_to_s3(image_bytes, student_id, year, college)
        except Exception as e:
            print(f"[MinIO S3] Upload failed, falling back to local storage: {e}")

    year_folder = os.path.join(upload_root, year)
    os.makedirs(year_folder, exist_ok=True)

    col_folder = _college_folder(college)
    college_folder = os.path.join(year_folder, col_folder)
    os.makedirs(college_folder, exist_ok=True)

    filename = f"{student_id}.jpg"
    full_path = os.path.join(college_folder, filename)

    with open(full_path, "wb") as f:
        f.write(image_bytes)

    # Optional background sync to Google Drive
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

    if s3_result and s3_result.get("url"):
        return {"path": s3_result["url"], "url": s3_result["url"]}

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
    Move old image to old/ subdirectory or S3 archive path.
    Also archives the old photo on Google Drive.
    Returns new relative path or S3 URL, or None if file not found.
    """
    if not old_rel_path:
        return None

    if is_s3_configured() and old_rel_path:
        try:
            s3_archived = archive_image_in_s3(old_rel_path, student_id)
            if s3_archived and (old_rel_path.startswith("http://") or old_rel_path.startswith("https://")):
                return s3_archived
        except Exception as e:
            print(f"[MinIO S3] Failed to archive S3 image: {e}")

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

    # 2. Trigger MinIO S3 move if configured
    if is_s3_configured() and old_rel_path:
        try:
            s3_moved = move_student_in_s3(old_rel_path, new_student_id, new_year, new_college)
            if s3_moved and (old_rel_path.startswith("http://") or old_rel_path.startswith("https://")):
                return s3_moved
        except Exception as e:
            print(f"[MinIO S3] Failed to move student in S3: {e}")

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