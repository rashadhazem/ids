import os
import io
import re
import json
from datetime import datetime
from dotenv import load_dotenv

try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload, MediaFileUpload
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False

load_dotenv()

# Google Drive Configuration Options:
# Option 1: Service Account (Recommended for servers)
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# Option 2: OAuth 2.0 User Credentials
CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")

# Root Folder ID on Google Drive (where student photos and backups will be saved)
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Folder ID cache to minimize API calls: { "parent_id/folder_name": "folder_id" }
_FOLDER_CACHE = {}


def is_gdrive_configured() -> bool:
    """Check if Google Drive credentials and folder ID are configured."""
    if not GOOGLE_LIBS_AVAILABLE:
        return False

    service_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    service_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    client_id    = os.getenv("GOOGLE_CLIENT_ID", "")
    client_sec   = os.getenv("GOOGLE_CLIENT_SECRET", "")
    ref_token    = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    folder_id    = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()

    has_service_account = bool(
        (service_file and os.path.exists(service_file)) or service_json
    )
    has_oauth = bool(client_id and client_sec and ref_token)
    return bool((has_service_account or has_oauth) and folder_id)


def get_drive_service():
    """Build and return an authorized Google Drive API service instance."""
    if not GOOGLE_LIBS_AVAILABLE:
        print("[GDrive] google-api-python-client is not installed.")
        return None

    service_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    service_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    client_id    = os.getenv("GOOGLE_CLIENT_ID", "")
    client_sec   = os.getenv("GOOGLE_CLIENT_SECRET", "")
    ref_token    = os.getenv("GOOGLE_REFRESH_TOKEN", "")

    creds = None

    # 1. Try Service Account JSON string from .env
    if service_json:
        try:
            info = json.loads(service_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            print(f"[GDrive] Error loading Service Account JSON string: {e}")

    # 2. Try Service Account JSON file path
    elif service_file and os.path.exists(service_file):
        try:
            creds = service_account.Credentials.from_service_account_file(service_file, scopes=SCOPES)
        except Exception as e:
            print(f"[GDrive] Error loading Service Account file: {e}")

    # 3. Try OAuth 2.0 User Credentials with Refresh Token
    elif client_id and client_sec and ref_token:
        try:
            creds = Credentials(
                None,
                refresh_token=ref_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_sec,
                scopes=SCOPES
            )
            if not creds.valid:
                creds.refresh(Request())
        except Exception as e:
            print(f"[GDrive] Error refreshing OAuth credentials: {e}")
            return None

    if not creds:
        return None

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return service
    except Exception as e:
        print(f"[GDrive] Failed to build Google Drive client service: {e}")
        return None


def _get_or_create_folder(service, folder_name: str, parent_id: str) -> str | None:
    """Get an existing folder by name inside parent_id or create a new one."""
    cache_key = f"{parent_id}/{folder_name}"
    if cache_key in _FOLDER_CACHE:
        return _FOLDER_CACHE[cache_key]

    try:
        # Search for folder with exact name inside parent
        escaped_name = folder_name.replace("'", "\\'")
        query = (
            f"name = '{escaped_name}' and '{parent_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        res = service.files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=1
        ).execute()
        files = res.get("files", [])
        if files:
            folder_id = files[0]["id"]
            _FOLDER_CACHE[cache_key] = folder_id
            return folder_id

        # Folder doesn't exist, create it
        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }
        folder = service.files().create(body=folder_metadata, fields="id").execute()
        folder_id = folder.get("id")
        _FOLDER_CACHE[cache_key] = folder_id
        return folder_id
    except Exception as e:
        print(f"[GDrive] Error getting/creating folder '{folder_name}': {e}")
        return None


def upload_to_gdrive(image_bytes: bytes, year: str, college: str, filename: str) -> bool:
    """
    Upload image bytes directly to Google Drive in the folder:
    {ROOT_FOLDER_ID}/{year}/{college_folder}/{filename}
    If file exists, it overwrites it.
    """
    if not is_gdrive_configured():
        return False

    service = get_drive_service()
    if not service:
        return False

    try:
        root_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
        if not root_folder_id:
            return False

        # Clean the college name for folder safety
        col_folder = re.sub(r'[\s/\\:*?"<>|]+', "_", college.strip()).rstrip("_") or "عام"

        # 1. Get/Create year folder inside root
        year_folder_id = _get_or_create_folder(service, str(year), root_folder_id)
        if not year_folder_id:
            return False

        # 2. Get/Create college folder inside year
        col_folder_id = _get_or_create_folder(service, col_folder, year_folder_id)
        if not col_folder_id:
            return False

        # 3. Check if file already exists in college folder to overwrite or create
        escaped_file = filename.replace("'", "\\'")
        query = (
            f"name = '{escaped_file}' and '{col_folder_id}' in parents "
            f"and trashed = false"
        )
        res = service.files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=1
        ).execute()
        files = res.get("files", [])

        media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg", resumable=True)

        if files:
            file_id = files[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
            print(f"[GDrive] Successfully updated: {year}/{col_folder}/{filename}")
        else:
            file_metadata = {
                "name": filename,
                "parents": [col_folder_id]
            }
            service.files().create(body=file_metadata, media_body=media, fields="id").execute()
            print(f"[GDrive] Successfully uploaded: {year}/{col_folder}/{filename}")

        return True
    except Exception as e:
        print(f"[GDrive] Error uploading '{filename}' to Google Drive: {e}")
        return False


def archive_in_gdrive(student_id: str, year: str, college: str, old_filename: str) -> bool:
    """
    Move/Rename existing photo {student_id}.jpg to {year}/{college_folder}/old/{old_filename}
    """
    if not is_gdrive_configured():
        return False

    service = get_drive_service()
    if not service:
        return False

    try:
        root_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
        if not root_folder_id:
            return False

        col_folder = re.sub(r'[\s/\\:*?"<>|]+', "_", college.strip()).rstrip("_") or "عام"

        # 1. Find year folder
        year_folder_id = _get_or_create_folder(service, str(year), root_folder_id)
        if not year_folder_id:
            return False

        # 2. Find college folder
        col_folder_id = _get_or_create_folder(service, col_folder, year_folder_id)
        if not col_folder_id:
            return False

        # 3. Find original file
        orig_name = f"{student_id}.jpg"
        escaped_orig = orig_name.replace("'", "\\'")
        query = f"name = '{escaped_orig}' and '{col_folder_id}' in parents and trashed = false"
        res = service.files().list(q=query, spaces="drive", fields="files(id, name)", pageSize=1).execute()
        files = res.get("files", [])
        if not files:
            print(f"[GDrive] File {orig_name} not found for archive; skipping.")
            return True

        file_id = files[0]["id"]

        # 4. Get/Create 'old' subfolder inside college folder
        old_folder_id = _get_or_create_folder(service, "old", col_folder_id)
        if not old_folder_id:
            return False

        # 5. Clean up any existing old photos for this student in 'old' folder
        # (ensures only the single previous photo is kept in old/)
        try:
            old_q = (
                f"(name = '{old_filename}' or name contains '{student_id}_old' or name = '{orig_name}') "
                f"and '{old_folder_id}' in parents and trashed = false"
            )
            old_res = service.files().list(q=old_q, spaces="drive", fields="files(id, name)").execute()
            for old_f in old_res.get("files", []):
                if old_f["id"] != file_id:
                    try:
                        service.files().delete(fileId=old_f["id"]).execute()
                        print(f"[GDrive] Removed prior old photo in old/ folder: {old_f['name']}")
                    except Exception:
                        pass
        except Exception as ce:
            print(f"[GDrive] Warning cleaning old archives: {ce}")

        # 6. Move file to 'old' folder and rename to old_filename
        service.files().update(
            fileId=file_id,
            addParents=old_folder_id,
            removeParents=col_folder_id,
            body={"name": old_filename},
            fields="id, parents, name"
        ).execute()

        print(f"[GDrive] Successfully archived: {orig_name} -> old/{old_filename}")
        return True
    except Exception as e:
        print(f"[GDrive] Error archiving '{student_id}' in Google Drive: {e}")
        return False


def upload_backup_to_gdrive(file_data: bytes | str, filename: str) -> bool:
    """
    Upload a database SQL dump, users Excel sheet, or archive file to Google Drive.
    Target Path: {ROOT_FOLDER_ID}/backups/{filename}
    Overwrites the single existing backup file so only one file is kept.
    """
    if not is_gdrive_configured():
        return False

    service = get_drive_service()
    if not service:
        return False

    try:
        root_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
        if not root_folder_id:
            return False

        # 1. Get or create 'backups' folder inside root
        backups_folder_id = _get_or_create_folder(service, "backups", root_folder_id)
        if not backups_folder_id:
            return False

        # Determine mimetype and media body
        if filename.endswith(".xlsx"):
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif filename.endswith(".tar.gz") or filename.endswith(".tgz"):
            mime = "application/gzip"
        elif filename.endswith(".sql"):
            mime = "application/sql"
        else:
            mime = "application/octet-stream"

        if isinstance(file_data, str) and os.path.exists(file_data):
            media = MediaFileUpload(file_data, mimetype=mime, resumable=True)
        elif isinstance(file_data, bytes):
            media = MediaIoBaseUpload(io.BytesIO(file_data), mimetype=mime, resumable=True)
        else:
            print("[GDrive] Invalid backup file data provided.")
            return False

        # 2. Check if file already exists in backups folder
        escaped_file = filename.replace("'", "\\'")
        query = (
            f"name = '{escaped_file}' and '{backups_folder_id}' in parents "
            f"and trashed = false"
        )
        res = service.files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=10
        ).execute()
        files = res.get("files", [])

        if files:
            file_id = files[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
            print(f"[GDrive] Successfully updated backup: backups/{filename}")
            # If there are duplicate files with the same name, remove extras
            for extra_f in files[1:]:
                try:
                    service.files().delete(fileId=extra_f["id"]).execute()
                except Exception:
                    pass
        else:
            file_metadata = {
                "name": filename,
                "parents": [backups_folder_id]
            }
            created_file = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
            file_id = created_file.get("id")
            print(f"[GDrive] Successfully uploaded backup: backups/{filename}")

        # 3. Clean up any leftover timestamped backup files (e.g., users_backup_*.xlsx)
        # to ensure only one backup file remains in the backups directory
        if filename.startswith("users_backup"):
            try:
                cleanup_q = f"'{backups_folder_id}' in parents and trashed = false"
                old_backups = service.files().list(q=cleanup_q, spaces="drive", fields="files(id, name)").execute()
                for ob in old_backups.get("files", []):
                    ob_name = ob.get("name", "")
                    # STRICT GUARD: Only delete old timestamped files (users_backup_*.xlsx)
                    # Never delete users_backup.xlsx or the active file_id
                    if ob.get("id") != file_id and ob_name != filename and ob_name.startswith("users_backup_"):
                        try:
                            service.files().delete(fileId=ob["id"]).execute()
                            print(f"[GDrive] Cleaned up legacy backup file: {ob_name}")
                        except Exception:
                            pass
            except Exception as ce:
                print(f"[GDrive] Error cleaning legacy backups: {ce}")

        return True
    except Exception as e:
        print(f"[GDrive] Error uploading backup '{filename}' to Google Drive: {e}")
        return False


def move_student_in_gdrive(
    old_student_id: str,
    old_year: str,
    old_college: str,
    new_student_id: str,
    new_year: str,
    new_college: str,
) -> bool:
    """
    Move a student's photo (and any archived photo) in Google Drive from:
      {old_year}/{old_college_folder}/{old_student_id}.jpg
    to:
      {new_year}/{new_college_folder}/{new_student_id}.jpg
    """
    if not is_gdrive_configured():
        return False

    service = get_drive_service()
    if not service:
        return False

    try:
        root_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
        if not root_folder_id:
            return False

        old_col_folder = re.sub(r'[\s/\\:*?"<>|]+', "_", (old_college or "").strip()).rstrip("_") or "عام"
        new_col_folder = re.sub(r'[\s/\\:*?"<>|]+', "_", (new_college or "").strip()).rstrip("_") or "عام"

        # 1. Target new folders (created if they don't exist yet)
        new_year_id = _get_or_create_folder(service, str(new_year), root_folder_id)
        if not new_year_id:
            return False
        new_col_id = _get_or_create_folder(service, new_col_folder, new_year_id)
        if not new_col_id:
            return False

        # 2. Source old folders
        old_year_id = _get_or_create_folder(service, str(old_year), root_folder_id)
        old_col_id = _get_or_create_folder(service, old_col_folder, old_year_id) if old_year_id else None

        if not old_col_id:
            print(f"[GDrive] Old college folder '{old_col_folder}' not found; skipping GDrive move.")
            return True

        # 3. Locate active photo in old college folder
        orig_name = f"{old_student_id}.jpg"
        escaped_name = orig_name.replace("'", "\\'")
        query = f"name = '{escaped_name}' and '{old_col_id}' in parents and trashed = false"
        res = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = res.get("files", [])

        new_filename = f"{new_student_id}.jpg"

        for f in files:
            file_id = f["id"]
            # Clean up any existing file with new_filename in target folder first
            escaped_new = new_filename.replace("'", "\\'")
            chk_q = f"name = '{escaped_new}' and '{new_col_id}' in parents and trashed = false"
            chk_res = service.files().list(q=chk_q, spaces="drive", fields="files(id)").execute()
            for ef in chk_res.get("files", []):
                if ef["id"] != file_id:
                    try:
                        service.files().delete(fileId=ef["id"]).execute()
                    except Exception:
                        pass

            # Move and rename
            service.files().update(
                fileId=file_id,
                addParents=new_col_id,
                removeParents=old_col_id,
                body={"name": new_filename},
                fields="id, name, parents"
            ).execute()
            print(f"[GDrive] Successfully moved student photo from {old_year}/{old_col_folder} to {new_year}/{new_col_folder}/{new_filename}")

        # 4. Check for archived photo in old/ folder
        old_sub_id = _get_or_create_folder(service, "old", old_col_id)
        if old_sub_id:
            old_pattern = f"{old_student_id}_old"
            arch_q = f"'{old_sub_id}' in parents and trashed = false"
            arch_res = service.files().list(q=arch_q, spaces="drive", fields="files(id, name)").execute()
            arch_files = [af for af in arch_res.get("files", []) if af.get("name", "").startswith(old_pattern)]

            if arch_files:
                new_sub_id = _get_or_create_folder(service, "old", new_col_id)
                new_old_name = f"{new_student_id}_old.jpg"
                for af in arch_files:
                    try:
                        service.files().update(
                            fileId=af["id"],
                            addParents=new_sub_id,
                            removeParents=old_sub_id,
                            body={"name": new_old_name},
                            fields="id, name, parents"
                        ).execute()
                        print(f"[GDrive] Successfully moved archived photo to {new_year}/{new_col_folder}/old/{new_old_name}")
                    except Exception as e:
                        print(f"[GDrive] Failed to move archived photo: {e}")

        return True
    except Exception as e:
        print(f"[GDrive] Error moving student {old_student_id} photo in Google Drive: {e}")
        return False


