from datetime import datetime, timedelta
from upload_cooldown import (is_upload_limit_error, in_cooldown, start_cooldown,
                             read_cooldown, write_cooldown)

NOW = datetime(2026, 7, 3, 23, 30, 0)


def test_detects_known_banner_text():
    assert is_upload_limit_error(
        "Невозможно загрузить bf693409.png. Максимальное количество загрузок 0 за раз")


def test_ignores_unrelated_text():
    assert is_upload_limit_error("Something went wrong while generating the response") is False
    assert is_upload_limit_error("") is False


def test_start_cooldown_then_in_cooldown_true(tmp_path):
    path = tmp_path / "cooldown_until.txt"
    start_cooldown(path, NOW, minutes=45)
    assert in_cooldown(path, NOW + timedelta(minutes=10)) is True


def test_in_cooldown_false_after_expiry(tmp_path):
    path = tmp_path / "cooldown_until.txt"
    start_cooldown(path, NOW, minutes=45)
    assert in_cooldown(path, NOW + timedelta(minutes=46)) is False


def test_in_cooldown_false_when_no_file(tmp_path):
    path = tmp_path / "missing.txt"
    assert in_cooldown(path, NOW) is False


def test_survives_process_restart_via_file(tmp_path):
    # запись и чтение — разные вызовы, как при перезапуске процесса
    path = tmp_path / "cooldown_until.txt"
    write_cooldown(path, NOW + timedelta(minutes=45))
    assert read_cooldown(path) == NOW + timedelta(minutes=45)
    assert in_cooldown(path, NOW + timedelta(minutes=1)) is True


def test_corrupted_file_treated_as_no_cooldown(tmp_path):
    path = tmp_path / "cooldown_until.txt"
    path.write_text("garbage-not-a-date", encoding="utf-8")
    assert read_cooldown(path) is None
    assert in_cooldown(path, NOW) is False
