"""Document storage service interface and local filesystem adapter."""

import hashlib
import os
import uuid
from pathlib import Path
from typing import Protocol

from app.config import settings


class StorageError(Exception):
    """Base exception for storage operations."""

    pass


class FileTooLargeError(StorageError):
    """Raised when uploaded file exceeds configured maximum size."""

    pass


class UnsupportedMediaTypeError(StorageError):
    """Raised when uploaded file MIME type is not allowed."""

    pass


def detect_mime_type_from_signature(content: bytes) -> str | None:
    """Detect supported file MIME type from magic bytes header."""
    if content.startswith(b"%PDF"):
        return "application/pdf"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


class DocumentStorage(Protocol):
    """Storage adapter interface for claim document binaries."""

    def store(self, file_name: str, content: bytes, mime_type: str) -> tuple[str, str, int]:
        """Store document bytes and return (storage_key, sha256, size_bytes)."""
        ...

    def retrieve(self, storage_key: str) -> bytes:
        """Retrieve binary content for a given storage key."""
        ...

    def get_file_path(self, storage_key: str) -> Path:
        """Get absolute filesystem path for streaming."""
        ...

    def delete(self, storage_key: str) -> None:
        """Delete stored binary."""
        ...


class LocalDocumentStorage:
    """Filesystem storage adapter storing documents under configured storage root."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.root_path = Path(root_dir or settings.UPLOAD_STORAGE_PATH).resolve()
        self.root_path.mkdir(parents=True, exist_ok=True)

    def _sanitize_extension(self, file_name: str) -> str:
        """Extract and validate clean lowercase extension."""
        clean_name = os.path.basename(file_name)
        suffix = Path(clean_name).suffix.lower()
        if suffix in (".pdf", ".jpg", ".jpeg", ".png"):
            return suffix
        return ".bin"

    def store(self, file_name: str, content: bytes, mime_type: str) -> tuple[str, str, int]:
        """Validate content, verify magic signature, store securely, and return metadata."""
        size_bytes = len(content)
        if size_bytes > settings.UPLOAD_MAX_SIZE_BYTES:
            raise FileTooLargeError(
                f"File size {size_bytes} bytes exceeds maximum allowed limit of {settings.UPLOAD_MAX_SIZE_BYTES} bytes"
            )

        clean_mime = mime_type.lower().split(";")[0].strip()
        if clean_mime not in settings.ALLOWED_UPLOAD_MIME_TYPES:
            raise UnsupportedMediaTypeError(
                f"MIME type '{mime_type}' is not allowed. Allowed types: {settings.ALLOWED_UPLOAD_MIME_TYPES}"
            )

        # Detect and verify file signature against declared MIME type (P1 task 6)
        detected_mime = detect_mime_type_from_signature(content)
        if not detected_mime:
            raise UnsupportedMediaTypeError(
                "File signature not recognized as a supported type (PDF, JPEG, PNG)"
            )

        normalized_declared = clean_mime
        if normalized_declared == "image/jpg":
            normalized_declared = "image/jpeg"
        if normalized_declared != detected_mime:
            raise UnsupportedMediaTypeError(
                f"Declared MIME type '{clean_mime}' does not match detected signature '{detected_mime}'"
            )

        # Compute SHA-256 digest
        sha256_hash = hashlib.sha256(content).hexdigest()

        # Generate secure random storage key (never derived from user input)
        ext = self._sanitize_extension(file_name)
        storage_key = f"{uuid.uuid4().hex}{ext}"
        destination = (self.root_path / storage_key).resolve()

        # Security check against directory traversal
        if not destination.is_relative_to(self.root_path):
            raise StorageError("Invalid storage target location")

        destination.write_bytes(content)
        return storage_key, sha256_hash, size_bytes

    def get_file_path(self, storage_key: str) -> Path:
        """Resolve and verify storage key path within storage root."""
        clean_key = os.path.basename(storage_key)
        target = (self.root_path / clean_key).resolve()
        if not target.is_relative_to(self.root_path) or not target.is_file():
            raise StorageError(f"File with storage key '{clean_key}' not found")
        return target

    def retrieve(self, storage_key: str) -> bytes:
        """Read and return document bytes."""
        path = self.get_file_path(storage_key)
        return path.read_bytes()

    def delete(self, storage_key: str) -> None:
        """Remove document file if it exists."""
        try:
            path = self.get_file_path(storage_key)
            if path.exists():
                path.unlink()
        except StorageError:
            pass


# Global default storage instance
default_storage = LocalDocumentStorage()
