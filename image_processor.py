import os
import io
import re
import shutil
from datetime import datetime
import numpy as np
import requests
import base64
import cv2
from PIL import Image, ImageOps

API_KEY = os.getenv("FACEPP_API_KEY", "")
API_SECRET = os.getenv("FACEPP_API_SECRET", "")

try:
    import cloudinary
    import cloudinary.uploader

    _CLD = bool(os.getenv("CLOUDINARY_CLOUD_NAME"))
    if _CLD:
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        )
except ImportError:
    _CLD = False

TARGET_W = 400
TARGET_H = 500
JPEG_Q = 88  # output quality

# OpenCV face detection cascades
_CASCADES = []
try:
    import cv2
    _CASCADE_PATHS = [
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
        cv2.data.haarcascades + "haarcascade_frontalface_alt.xml",
        cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml",
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


def detect_faces_opencv(image_bytes: bytes) -> list[tuple[int, int, int, int]]:
    """Fallback face detection using OpenCV cascades."""
    if not _CASCADES:
        return []
    
    cv_img = _bytes_to_cv(image_bytes)
    if cv_img is None:
        return []

    # Downscale for speed
    orig_h, orig_w = cv_img.shape[:2]
    max_dim = 1000
    if max(orig_h, orig_w) > max_dim:
        sc = max_dim / max(orig_h, orig_w)
        cv_img = cv2.resize(cv_img, (int(orig_w * sc), int(orig_h * sc)))

    det_h, det_w = cv_img.shape[:2]
    scale_x = orig_w / det_w
    scale_y = orig_h / det_h

    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    faces = []
    for cascade in _CASCADES:
        detected = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
        )
        if len(detected) > 0:
            # Scale back to original coordinates
            scaled = [(int(x*scale_x), int(y*scale_y), int(w*scale_x), int(h*scale_y))
                      for (x, y, w, h) in detected]
            faces.extend(scaled)
            break  # Use first successful cascade

    return faces


def detect_face_faceplusplus(image_bytes: bytes) -> list:
    """Detect faces using the Face++ API."""
    encoded_image = base64.b64encode(image_bytes).decode("utf-8")

    if not API_KEY or not API_SECRET:
        print("Face++ credentials not configured; skipping Face++ detection.")
        return []

    url = "https://api-us.faceplusplus.com/facepp/v3/detect"
    data = {
        "api_key": API_KEY,
        "api_secret": API_SECRET,
        "image_base64": encoded_image,
    }

    try:
        response = requests.post(url, data=data, timeout=20)
        print("Face++ status:", response.status_code)
        print("Face++ response:", response.text)

        result = response.json()
        return result.get("faces", [])
    except Exception as e:
        print("Face++ request error:", str(e))
        return []


def detect_faces(image_bytes: bytes) -> list[tuple[int, int, int, int]]:
    """
    Return list of (x, y, w, h) face rectangles.
    Try Face++ API first, fallback to OpenCV if no faces found.
    """
    normalized_bytes = _normalize_image_bytes(image_bytes)

    # Try Face++ first
    faces = detect_face_faceplusplus(normalized_bytes)
    if faces:
        return [
            (
                int(face["face_rectangle"]["left"]),
                int(face["face_rectangle"]["top"]),
                int(face["face_rectangle"]["width"]),
                int(face["face_rectangle"]["height"]),
            )
            for face in faces
        ]

    # Fallback to OpenCV
    return detect_faces_opencv(normalized_bytes)


def face_detected(image_bytes: bytes) -> bool:
    faces = detect_faces(image_bytes)
    print("Faces detected:", faces)
    return len(faces) > 0


def smart_crop_face(image_bytes: bytes) -> bytes:
    """
    Auto-crop the image centered on the largest detected face.
    Adds headroom above and body below.
    Returns 400x500 JPEG bytes.
    If no face is found, returns a centered crop.
    """
    normalized_bytes = _normalize_image_bytes(image_bytes)
    pil = Image.open(io.BytesIO(normalized_bytes)).convert("RGB")

    faces = detect_faces(normalized_bytes)
    iw, ih = pil.size

    if faces:
        fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
        face_cx = fx + fw // 2
        face_cy = fy + fh // 2

        crop_h = int(fh * 3.5)
        crop_w = int(crop_h * TARGET_W / TARGET_H)

        top = face_cy - int(crop_h * 0.28)
        left = face_cx - crop_w // 2
        if crop_w > iw or crop_h > ih:
            top, left, crop_w, crop_h = 0, 0, iw, ih
        else:
            top = max(0, min(top, ih - crop_h))
            left = max(0, min(left, iw - crop_w))
    else:
        ar = TARGET_W / TARGET_H
        if iw / ih > ar:
            crop_w = int(ih * ar)
            crop_h = ih
        else:
            crop_h = int(iw / ar)
            crop_w = iw
        left = (iw - crop_w) // 2
        top = (ih - crop_h) // 2

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
        return smart_crop_face(edited_bytes)

    pil = pil.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    return _pil_to_bytes(pil)


def _college_folder(college: str) -> str:
    """Return a safe folder name for the college (Arabic-friendly, spaces→underscore)."""
    safe = re.sub(r'[\s/\\:*?"<>|]+', "_", college.strip())
    return safe.rstrip("_") or "عام"


def save_image(
    image_bytes: bytes, student_id: str, year: str, college: str, upload_root: str
) -> dict:
    """
    Save processed image if it contains a human face.
    Local path: uploads/{year}/{college}/{student_id}.jpg
    Returns { "path": relative_path, "url": public_url }
    """
    if not face_detected(image_bytes):
        raise ValueError("الصورة لا تحتوي على وجه بشري. الصورة غير مطابقة للمعايير.")

    if _CLD:
        col_slug = _college_folder(college)
        result = cloudinary.uploader.upload(
            image_bytes,
            public_id=f"students/{year}/{col_slug}/{student_id}",
            overwrite=True,
            resource_type="image",
            transformation=[
                {
                    "width": TARGET_W,
                    "height": TARGET_H,
                    "crop": "fill",
                    "gravity": "face",
                }
            ],
        )
        return {
            "path": result["public_id"],
            "url": result["secure_url"],
            "cloudinary": True,
        }

    year_folder = os.path.join(upload_root, year)
    os.makedirs(year_folder, exist_ok=True)

    col_folder = _college_folder(college)
    college_folder = os.path.join(year_folder, col_folder)
    os.makedirs(college_folder, exist_ok=True)

    filename = f"{student_id}.jpg"
    full_path = os.path.join(college_folder, filename)

    with open(full_path, "wb") as f:
        f.write(image_bytes)

    rel_path = os.path.relpath(full_path, os.path.join(upload_root, "..")).replace(
        "\\", "/"
    )
    return {"path": rel_path, "url": f"/static/{rel_path}", "cloudinary": False}


def archive_old_image(
    old_rel_path: str, student_id: str, static_root: str, upload_root: str
) -> str | None:
    """
    Move old image to old/ subdirectory with timestamp.
    Returns new relative path, or None if file not found.
    """
    if not old_rel_path:
        return None
    old_full_path = os.path.join(static_root, old_rel_path)
    if not os.path.exists(old_full_path):
        return None

    # Create old/ subdirectory in the same directory as the image
    image_dir = os.path.dirname(old_full_path)
    old_dir = os.path.join(image_dir, "old")
    os.makedirs(old_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{student_id}_old_{ts}.jpg"
    new_full_path = os.path.join(old_dir, filename)

    try:
        shutil.move(old_full_path, new_full_path)
        rel_path = os.path.relpath(new_full_path, static_root).replace("\\", "/")
        return rel_path
    except Exception as e:
        print(f"Error archiving image: {e}")
        return None