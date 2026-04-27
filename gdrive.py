"""Загрузка результатов на Google Drive через OAuth2 токен пользователя."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("gdrive")

TOKEN_FILE = Path(__file__).parent / "gdrive_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Токен не найден: {TOKEN_FILE}. Запустите python gdrive_auth.py"
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def upload_file(file_path: Path, folder_id: str, credentials_json: str) -> str:
    """Загружает файл в папку Google Drive. Возвращает webViewLink."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    meta = {"name": file_path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(file_path), mimetype="image/png", resumable=False)
    f = (
        service.files()
        .create(body=meta, media_body=media, fields="id,webViewLink")
        .execute()
    )
    return f["webViewLink"]
