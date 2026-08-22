"""Validates and persists uploaded audio to disk.

Storage is content-addressed: a file is written under its own sha256
hash, not the client-supplied name. That sidesteps path-traversal and
filename-collision concerns entirely, and doubles as the dedupe key
that database.meetings_repo.find_completed_by_hash relies on instead
of a Redis cache.
"""

import hashlib
import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from backend.app.core import config

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a"}

# Enough to catch a mislabeled/renamed file — not a full magic-byte library.
_SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
    ".mp3": [(0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"\xff\xf2")],
    ".wav": [(0, b"RIFF")],
    ".m4a": [(4, b"ftyp")],
}

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_CHUNK_SIZE = 1024 * 1024


class UploadValidationError(ValueError):
    """Raised when an uploaded file fails validation. Message is user-facing."""


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "audio").name
    stem, suffix = Path(name).stem, Path(name).suffix
    stem = _SAFE_CHARS.sub("_", stem).strip("._") or "audio"
    suffix = _SAFE_CHARS.sub("_", suffix)
    return f"{stem}{suffix}"


def _validate_extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UploadValidationError(f"Unsupported file type '{ext or 'unknown'}'. Allowed: {allowed}.")
    return ext


def _validate_signature(head: bytes, ext: str) -> None:
    for offset, magic in _SIGNATURES.get(ext, []):
        if head[offset : offset + len(magic)] == magic:
            return
    raise UploadValidationError(
        f"File content doesn't look like a valid {ext} file "
        "— the extension may not match the actual format."
    )


def save_upload(upload: UploadFile) -> tuple[str, str, str]:
    """Validate and persist an uploaded audio file.

    Returns (safe_filename, file_hash, audio_path).
    Raises UploadValidationError on any validation failure; the
    exception message is safe to show to the user as-is.
    """
    ext = _validate_extension(upload.filename or "")
    safe_filename = sanitize_filename(upload.filename or f"audio{ext}")

    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = config.AUDIO_DIR / f".tmp-{uuid.uuid4().hex}"

    digest = hashlib.sha256()
    total_bytes = 0
    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    head = b""

    try:
        with tmp_path.open("wb") as out:
            while chunk := upload.file.read(_CHUNK_SIZE):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise UploadValidationError(
                        f"File exceeds the {config.MAX_UPLOAD_MB}MB upload limit."
                    )
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                digest.update(chunk)
                out.write(chunk)

        if total_bytes == 0:
            raise UploadValidationError("Uploaded file is empty.")

        _validate_signature(head, ext)

        file_hash = digest.hexdigest()
        final_path = config.AUDIO_DIR / f"{file_hash}{ext}"
        if final_path.exists():
            tmp_path.unlink()
        else:
            tmp_path.rename(final_path)

        return safe_filename, file_hash, str(final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
