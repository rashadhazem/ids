import os
import io
import re
import json
from datetime import datetime
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

# Google Drive Configuration Options:
# Option 1: Service Account (Recommended for servers)
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# Option 2: OAuth 2.0 User Credentials
CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")

# Root Folder ID on Google Drive (where student photos will be saved)
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Folder ID cache to minimize API calls: { "parent_id/folder_name": "folder_id" }
_FOLDER_CACHE = {}


def is_gdrive_configured() -> bool:
    """Check if Google Drive credentials and folder ID are configured."""
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
        query = (
            f"name = '{folder_name}' and '{parent_id}' in parents "
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
        query = (
            f"name = '{filename}' and '{col_folder_id}' in parents "
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
        query = f"name = '{orig_name}' and '{col_folder_id}' in parents and trashed = false"
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

        # 5. Move file to 'old' folder and rename to old_filename
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
