"""Тесты загрузки файлов на Google Drive.

Запуск:
    pytest tests/test_gdrive.py -v

Интеграционный тест (test_real_upload) требует наличия gdrive_token.json
и сетевого доступа. Пропускается автоматически если токен не найден.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─── Пути ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent  # agent_convert_foto_rituailb2b2/
_AGENT_DIR = _ROOT

for _p in (_AGENT_DIR,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TOKEN_FILE = _AGENT_DIR / "gdrive_token.json"
RITUAL_FOLDER_ID = "1C-G2kGAi5HIr4Hj45MDNZGC-IWvknbvu"


# ══════════════════════════════════════════════════════════════════════
# 1. Unit-тесты (без сети)
# ══════════════════════════════════════════════════════════════════════

def test_upload_file_raises_if_no_token(tmp_path):
    """upload_file бросает FileNotFoundError если токен отсутствует."""
    import gdrive as _gdrive
    original = _gdrive.TOKEN_FILE
    _gdrive.TOKEN_FILE = tmp_path / "no_token.json"
    try:
        fake_file = tmp_path / "test.png"
        fake_file.write_bytes(b"\x89PNG\r\n")
        with pytest.raises(FileNotFoundError, match="Токен не найден"):
            _gdrive.upload_file(fake_file, RITUAL_FOLDER_ID, "")
    finally:
        _gdrive.TOKEN_FILE = original


def test_upload_file_calls_drive_api(tmp_path):
    """upload_file обращается к Drive API с правильными параметрами."""
    import gdrive as _gdrive

    fake_file = tmp_path / "result.png"
    fake_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    # Мокаем credentials и Drive API
    mock_creds = MagicMock()
    mock_creds.expired = False

    mock_file_resp = {"id": "FILE_ID_123", "webViewLink": "https://drive.google.com/file/d/FILE_ID_123/view"}
    mock_service = MagicMock()
    (mock_service.files().create().execute.return_value) = mock_file_resp

    with patch.object(_gdrive, "_get_credentials", return_value=mock_creds), \
         patch("googleapiclient.discovery.build", return_value=mock_service):
        link = _gdrive.upload_file(fake_file, RITUAL_FOLDER_ID, "")

    assert link == "https://drive.google.com/file/d/FILE_ID_123/view"
    # Проверяем что передали правильный folder_id
    call_kwargs = mock_service.files().create.call_args
    body = call_kwargs[1]["body"] if call_kwargs[1] else call_kwargs[0][0]
    assert body["parents"] == [RITUAL_FOLDER_ID]
    assert body["name"] == "result.png"


# ══════════════════════════════════════════════════════════════════════
# 2. Интеграционный тест (реальная загрузка в Drive)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not TOKEN_FILE.exists(),
    reason="gdrive_token.json не найден — интеграционный тест пропущен",
)
def test_real_upload_and_delete(tmp_path):
    """Загружает реальный PNG в Drive и удаляет его после теста."""
    import gdrive as _gdrive
    from googleapiclient.discovery import build

    # Создаём минимальный PNG (1x1 пиксель, белый)
    png_bytes = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00'
        b'\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18'
        b'\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    test_file = tmp_path / "test_gdrive_upload.png"
    test_file.write_bytes(png_bytes)

    # Загружаем в папку ritual (корзинки)
    link = _gdrive.upload_file(test_file, RITUAL_FOLDER_ID, "")
    assert link.startswith("https://drive.google.com/"), f"Неверный URL: {link}"
    print(f"\n✅ Файл загружен: {link}")

    # Удаляем тестовый файл из Drive
    creds = _gdrive._get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    # Ищем файл по имени в папке
    results = service.files().list(
        q=f"name='test_gdrive_upload.png' and '{RITUAL_FOLDER_ID}' in parents and trashed=false",
        fields="files(id,name)",
    ).execute()
    for f in results.get("files", []):
        service.files().delete(fileId=f["id"]).execute()
        print(f"🗑️  Удалён тестовый файл: {f['id']}")


# ══════════════════════════════════════════════════════════════════════
# 3. Тест конфига — все режимы имеют folder_id
# ══════════════════════════════════════════════════════════════════════

def test_all_modes_have_gdrive_folder_id():
    """Все 5 режимов имеют GDRIVE_FOLDER_ID из .env."""
    from dotenv import load_dotenv
    env_path = _AGENT_DIR / ".env"
    if not env_path.exists():
        pytest.skip(".env не найден")
    load_dotenv(env_path, override=True)

    import importlib
    import config
    importlib.reload(config)

    modes = config.MODES  # dict[str, Mode]
    missing = [key for key, m in modes.items() if not m.gdrive_folder_id]
    assert not missing, (
        f"Режимы без GDRIVE_FOLDER_ID: {missing}\n"
        "Проверь .env — должны быть RITUAL_GDRIVE_FOLDER_ID, WREATH_GDRIVE_FOLDER_ID и т.д."
    )
    for key, m in modes.items():
        print(f"  {key}: {m.gdrive_folder_id}")
