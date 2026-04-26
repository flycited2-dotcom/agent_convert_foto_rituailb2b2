"""Clipboard helpers for Windows.

Image: copy via PIL + pywin32 BITMAP/DIB. ChatGPT понимает paste картинки.
Text: через ctypes WinAPI (надёжнее чем subprocess в PowerShell).
"""
from __future__ import annotations

from pathlib import Path
from io import BytesIO

import win32clipboard
import win32con
from PIL import Image


def copy_image_to_clipboard(path: str | Path) -> None:
    """Копирует изображение в clipboard как DIB (Device Independent Bitmap).

    Это формат, который ChatGPT понимает при Ctrl+V в чате.
    """
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    buf = BytesIO()
    img.save(buf, format="BMP")
    data = buf.getvalue()[14:]  # BMP file header is 14 bytes — отбрасываем

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()


def copy_text_to_clipboard(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()
