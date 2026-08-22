import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from backend.app.core import config
from backend.app.services.storage_service import (
    UploadValidationError,
    sanitize_filename,
    save_upload,
)


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


def test_sanitize_filename_strips_path_and_unsafe_chars():
    assert sanitize_filename("../../etc/passwd.mp3") == "passwd.mp3"
    assert sanitize_filename("my meeting notes!!.mp3") == "my_meeting_notes.mp3"


def test_save_upload_rejects_unsupported_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)

    with pytest.raises(UploadValidationError, match="Unsupported file type"):
        save_upload(_upload("meeting.exe", b"whatever"))


def test_save_upload_rejects_mismatched_content(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)

    with pytest.raises(UploadValidationError, match="doesn't look like"):
        save_upload(_upload("meeting.mp3", b"not actually an mp3 file"))


def test_save_upload_rejects_empty_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)

    with pytest.raises(UploadValidationError, match="empty"):
        save_upload(_upload("meeting.mp3", b""))


def test_save_upload_enforces_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)

    with pytest.raises(UploadValidationError, match="exceeds"):
        save_upload(_upload("meeting.mp3", b"ID3" + b"0" * 1000))


def test_save_upload_accepts_valid_mp3(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)
    content = b"ID3" + b"\x00" * 100

    filename, file_hash, audio_path = save_upload(_upload("My Meeting.mp3", content))

    assert filename == "My_Meeting.mp3"
    assert len(file_hash) == 64
    assert Path(audio_path).read_bytes() == content


def test_save_upload_dedupes_identical_content(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)
    content = b"RIFF" + b"\x00" * 50

    _, hash_a, path_a = save_upload(_upload("a.wav", content))
    _, hash_b, path_b = save_upload(_upload("b.wav", content))

    assert hash_a == hash_b
    assert path_a == path_b
    assert len(list(tmp_path.iterdir())) == 1
