import io
import json
import os
import re
from dotenv import load_dotenv

load_dotenv()

try:
    from minio import Minio
    from minio.error import S3Error
    MINIO_LIB_AVAILABLE = True
except ImportError:
    MINIO_LIB_AVAILABLE = False

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000").strip()
MINIO_ACCESS_KEY = os.getenv("MINIO_ROOT_USER", "bua_minio_admin").strip()
MINIO_SECRET_KEY = os.getenv("MINIO_ROOT_PASSWORD", "BuaMinioSecure@2026!").strip()
MINIO_BUCKET = os.getenv("MINIO_BUCKET_NAME", "student-photos").strip()
MINIO_PUBLIC_URL = os.getenv("MINIO_PUBLIC_URL", "https://id-storage.devhubai.net").strip().rstrip("/")
MINIO_SECURE = os.getenv("MINIO_USE_SSL", "false").strip().lower() in ("true", "1", "yes")

_client = None
_bucket_checked = False


def _safe_folder(name: str) -> str:
    safe = re.sub(r'[\s/\\:*?"<>|]+', "_", name.strip())
    return safe.rstrip("_") or "عام"


def get_s3_client():
    global _client
    if _client is not None:
        return _client
    if not MINIO_LIB_AVAILABLE or not MINIO_ENDPOINT:
        return None

    endpoint = MINIO_ENDPOINT
    if endpoint.startswith("http://"):
        endpoint = endpoint[7:]
    elif endpoint.startswith("https://"):
        endpoint = endpoint[8:]

    try:
        _client = Minio(
            endpoint=endpoint,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
        return _client
    except Exception as e:
        print(f"[MinIO] Failed to initialize client: {e}")
        return None


def is_s3_configured() -> bool:
    return bool(MINIO_LIB_AVAILABLE and MINIO_ENDPOINT and MINIO_ACCESS_KEY and MINIO_SECRET_KEY)


def ensure_bucket_exists(bucket_name: str = None) -> bool:
    global _bucket_checked
    target_bucket = bucket_name or MINIO_BUCKET
    client = get_s3_client()
    if not client:
        return False

    try:
        if not client.bucket_exists(target_bucket):
            client.make_bucket(target_bucket)
            print(f"[MinIO] Created bucket: {target_bucket}")

        # Set anonymous download policy so photos load directly in browsers via HTTPS
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
                    "Resource": [f"arn:aws:s3:::{target_bucket}"],
                },
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{target_bucket}/*"],
                },
            ],
        }
        client.set_bucket_policy(target_bucket, json.dumps(policy))
        _bucket_checked = True
        return True
    except Exception as e:
        print(f"[MinIO] Bucket check/policy error for '{target_bucket}': {e}")
        return False


def _extract_object_key(path_or_url: str) -> str:
    if not path_or_url:
        return ""
    clean = path_or_url.replace("\\", "/")
    if clean.startswith("http://") or clean.startswith("https://"):
        parts = clean.split("/")
        # URL pattern: https://domain/bucket/key...
        if len(parts) >= 5 and parts[3] == MINIO_BUCKET:
            return "/".join(parts[4:])
        return "/".join(parts[3:])
    if clean.startswith(f"{MINIO_BUCKET}/"):
        return clean[len(MINIO_BUCKET) + 1:]
    if clean.startswith("uploads/"):
        return clean[len("uploads/"):]
    if clean.startswith("/static/uploads/"):
        return clean[len("/static/uploads/"):]
    return clean.lstrip("/")


def upload_image_to_s3(image_bytes: bytes, student_id: str, year: str, college: str) -> dict:
    client = get_s3_client()
    if not client:
        raise RuntimeError("MinIO S3 client is not available")

    global _bucket_checked
    if not _bucket_checked:
        ensure_bucket_exists()

    col_folder = _safe_folder(college)
    object_name = f"{year}/{col_folder}/{student_id}.jpg"

    client.put_object(
        bucket_name=MINIO_BUCKET,
        object_name=object_name,
        data=io.BytesIO(image_bytes),
        length=len(image_bytes),
        content_type="image/jpeg",
    )

    public_url = f"{MINIO_PUBLIC_URL}/{MINIO_BUCKET}/{object_name}"
    return {
        "path": public_url,
        "url": public_url,
        "key": object_name,
    }


def archive_image_in_s3(image_path_or_url: str, student_id: str) -> str | None:
    client = get_s3_client()
    if not client:
        return None

    old_key = _extract_object_key(image_path_or_url)
    if not old_key:
        return None

    try:
        from minio.commonconfig import CopySource
        key_parts = old_key.split("/")
        if len(key_parts) >= 3:
            year, col = key_parts[0], key_parts[1]
            archive_key = f"{year}/{col}/old/{student_id}_old.jpg"
        else:
            archive_key = f"old/{student_id}_old.jpg"

        client.copy_object(
            bucket_name=MINIO_BUCKET,
            object_name=archive_key,
            source=CopySource(MINIO_BUCKET, old_key),
        )
        try:
            client.remove_object(MINIO_BUCKET, old_key)
        except Exception:
            pass

        return f"{MINIO_PUBLIC_URL}/{MINIO_BUCKET}/{archive_key}"
    except Exception as e:
        print(f"[MinIO] Failed to archive S3 image: {e}")
        return None


def move_student_in_s3(
    old_image_path_or_url: str,
    new_student_id: str,
    new_year: str,
    new_college: str,
) -> str | None:
    client = get_s3_client()
    if not client:
        return None

    old_key = _extract_object_key(old_image_path_or_url)
    if not old_key:
        return None

    new_col_folder = _safe_folder(new_college)
    new_key = f"{new_year}/{new_col_folder}/{new_student_id}.jpg"

    if old_key == new_key:
        return f"{MINIO_PUBLIC_URL}/{MINIO_BUCKET}/{new_key}"

    try:
        from minio.commonconfig import CopySource
        client.copy_object(
            bucket_name=MINIO_BUCKET,
            object_name=new_key,
            source=CopySource(MINIO_BUCKET, old_key),
        )
        try:
            client.remove_object(MINIO_BUCKET, old_key)
        except Exception:
            pass
        return f"{MINIO_PUBLIC_URL}/{MINIO_BUCKET}/{new_key}"
    except Exception as e:
        print(f"[MinIO] Failed to move S3 object: {e}")
        return None


def delete_image_from_s3(image_path_or_url: str) -> bool:
    client = get_s3_client()
    if not client:
        return False

    key = _extract_object_key(image_path_or_url)
    if not key:
        return False

    try:
        client.remove_object(MINIO_BUCKET, key)
        return True
    except Exception as e:
        print(f"[MinIO] Delete error for '{key}': {e}")
        return False


def get_image_bytes(image_path_or_url: str) -> bytes | None:
    if not image_path_or_url:
        return None

    client = get_s3_client()
    if client:
        key = _extract_object_key(image_path_or_url)
        if key:
            try:
                response = client.get_object(MINIO_BUCKET, key)
                data = response.read()
                response.close()
                response.release_conn()
                return data
            except Exception:
                pass

    if image_path_or_url.startswith("http://") or image_path_or_url.startswith("https://"):
        try:
            import requests
            r = requests.get(image_path_or_url, timeout=10)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass

    return None
