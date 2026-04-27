"""Одноразовая авторизация Google Drive. Запустить один раз:
    python gdrive_auth.py
Откроется браузер, войдите в аккаунт Google — токен сохранится в gdrive_token.json.
"""
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_FILE = next(Path(__file__).parent.glob("client_secret_*.json"), Path(__file__).parent / "gdrive_oauth_client.json")
TOKEN_FILE = Path(__file__).parent / "gdrive_token.json"

if not CLIENT_FILE.exists():
    print(f"Файл не найден: {CLIENT_FILE}")
    print("Скачайте OAuth client JSON из Google Cloud Console и положите рядом.")
    raise SystemExit(1)

flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
creds = flow.run_local_server(port=0)
TOKEN_FILE.write_text(creds.to_json())
print(f"\nТокен сохранён: {TOKEN_FILE}")
print("Теперь загрузка на Google Drive будет работать автоматически.")
