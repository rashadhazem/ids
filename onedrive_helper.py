import os
import requests
import re
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.getenv("ONEDRIVE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("ONEDRIVE_CLIENT_SECRET", "")
TENANT_ID     = os.getenv("ONEDRIVE_TENANT_ID", "common")
REFRESH_TOKEN = os.getenv("ONEDRIVE_REFRESH_TOKEN", "")
ROOT_FOLDER   = os.getenv("ONEDRIVE_ROOT_FOLDER", "badr_university")

# Gating: only activate if Client ID, Secret, and Refresh Token are configured
ENABLED = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)

def get_access_token() -> str | None:
    """Exchange the offline refresh token for a short-lived access token."""
    if not ENABLED:
        return None

    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "scope": "Files.ReadWrite.All offline_access",
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
        "client_secret": CLIENT_SECRET,
    }
    
    try:
        res = requests.post(url, data=data, timeout=15)
        if res.status_code == 200:
            return res.json().get("access_token")
        else:
            print(f"[OneDrive] Token exchange failed ({res.status_code}): {res.text}")
            return None
    except Exception as e:
        print(f"[OneDrive] Connection error during token exchange: {e}")
        return None


def upload_to_onedrive(image_bytes: bytes, year: str, college: str, filename: str) -> bool:
    """
    Upload file bytes directly to the specified OneDrive folder structure.
    Path: {ROOT_FOLDER}/{year}/{college_slug}/{filename}
    """
    if not ENABLED:
        return False

    token = get_access_token()
    if not token:
        return False

    # Clean the college name for URL/Path compatibility
    col_slug = re.sub(r'[\s/\\:*?"<>|]+', "_", college.strip()).rstrip("_") or "عام"
    
    # Path construction in OneDrive
    # Format: /me/drive/root:/badr_university/2026/كلية_التمريض/2026000000.jpg:/content
    dest_path = f"{ROOT_FOLDER}/{year}/{col_slug}/{filename}"
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{dest_path}:/content"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "image/jpeg"
    }

    try:
        res = requests.put(url, headers=headers, data=image_bytes, timeout=30)
        if res.status_code in (200, 201):
            print(f"[OneDrive] Successfully uploaded: {dest_path}")
            return True
        else:
            print(f"[OneDrive] Upload failed ({res.status_code}): {res.text}")
            return False
    except Exception as e:
        print(f"[OneDrive] Connection error during upload: {e}")
        return False


def archive_in_onedrive(student_id: str, year: str, college: str, old_filename: str) -> bool:
    """
    Move/Rename an old image inside OneDrive to the 'old/' subfolder.
    From: {ROOT_FOLDER}/{year}/{college_slug}/{student_id}.jpg
    To: {ROOT_FOLDER}/{year}/{college_slug}/old/{student_id}_old_{timestamp}.jpg
    """
    if not ENABLED:
        return False

    token = get_access_token()
    if not token:
        return False

    col_slug = re.sub(r'[\s/\\:*?"<>|]+', "_", college.strip()).rstrip("_") or "عام"
    
    # Define source path and target folder path
    src_path = f"{ROOT_FOLDER}/{year}/{col_slug}/{student_id}.jpg"
    target_parent_path = f"/drive/root:/{ROOT_FOLDER}/{year}/{col_slug}/old"
    
    # Graph API Endpoint to patch metadata of the existing item
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{src_path}"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Request payload specifying target folder path and target name
    body = {
        "parentReference": {
            "path": target_parent_path
        },
        "name": old_filename
    }

    try:
        res = requests.patch(url, headers=headers, json=body, timeout=20)
        if res.status_code == 200:
            print(f"[OneDrive] Successfully archived old image: {old_filename}")
            return True
        elif res.status_code == 404:
            # File was not found in OneDrive; nothing to archive
            print(f"[OneDrive] Old file {src_path} not found; skipping archive.")
            return True
        else:
            print(f"[OneDrive] Archiving failed ({res.status_code}): {res.text}")
            return False
    except Exception as e:
        print(f"[OneDrive] Connection error during archiving: {e}")
        return False
