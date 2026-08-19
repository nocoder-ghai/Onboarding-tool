"""Object storage for headshots, videos and policy documents.

`save()` / `open_path()` / `delete()` are the only entry points the rest of the app
uses, so swapping the local backend for S3/GCS is a change confined to this file
(see `_LocalBackend` and the note in the README).
"""

import os
import re
import uuid

from . import config

# --------------------------------------------------------------------------- #
# Format rules
# --------------------------------------------------------------------------- #

IMAGE_TYPES = {
    "image/jpeg": [".jpg", ".jpeg"],
    "image/png": [".png"],
    "image/webp": [".webp"],
}
VIDEO_TYPES = {
    "video/mp4": [".mp4", ".m4v"],
    "video/quicktime": [".mov"],
    "video/webm": [".webm"],
}
DOC_TYPES = {
    "application/pdf": [".pdf"],
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": [".pptx"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
    "application/msword": [".doc"],
    "application/vnd.ms-powerpoint": [".ppt"],
}

# Leading bytes we trust more than the browser-supplied Content-Type.
MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # pptx/docx are zip containers
]

PROFILE_KINDS = {
    "image": (IMAGE_TYPES, config.MAX_IMAGE_MB, "a JPG, PNG or WebP image"),
    "video": (VIDEO_TYPES, config.MAX_VIDEO_MB, "an MP4, MOV or WebM video"),
    "document": (DOC_TYPES, config.MAX_DOC_MB, "a PDF, PPTX or DOCX file"),
}


class ValidationError(Exception):
    pass


def _sniff(data):
    for prefix, mime in MAGIC:
        if data.startswith(prefix):
            return mime
    if data[4:12] in (b"ftypmp42", b"ftypisom", b"ftypM4V ") or data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    return None


def kind_for_mime(mime):
    for name, (table, _, _) in PROFILE_KINDS.items():
        if mime in table:
            return name
    return None


def validate(upload, profile, max_mb=None):
    """Check an UploadedFile against a profile ('image'|'video'|'document').

    Returns the resolved mime type, or raises ValidationError with copy that is
    safe to show a tutor.
    """
    if profile not in PROFILE_KINDS:
        raise ValidationError("Unsupported upload type.")
    allowed, default_mb, human = PROFILE_KINDS[profile]
    limit_mb = max_mb or default_mb

    if not upload or not upload.data:
        raise ValidationError("Please choose a file first.")
    if upload.size > limit_mb * 1024 * 1024:
        raise ValidationError("That file is %.1f MB. Please keep it under %d MB."
                              % (upload.size / 1048576.0, limit_mb))

    ext = os.path.splitext(upload.filename or "")[1].lower()
    valid_exts = {e for exts in allowed.values() for e in exts}
    if ext not in valid_exts:
        raise ValidationError("Please upload %s." % human)

    declared = (upload.content_type or "").split(";")[0].strip().lower()
    sniffed = _sniff(upload.data[:32])
    # A zip signature is expected for pptx/docx; trust the extension there.
    if sniffed == "application/zip":
        sniffed = None
    if sniffed and sniffed not in allowed:
        raise ValidationError("That file doesn't look like %s." % human)
    if declared in allowed:
        return declared
    if sniffed:
        return sniffed
    for mime, exts in allowed.items():
        if ext in exts:
            return mime
    raise ValidationError("Please upload %s." % human)


def validate_any(upload, profiles, max_mb=None):
    """Accept a file matching any of several profiles (e.g. a deck or a video)."""
    last = None
    for profile in profiles:
        try:
            return validate(upload, profile, max_mb)
        except ValidationError as exc:
            last = exc
    humans = [PROFILE_KINDS[p][2] for p in profiles if p in PROFILE_KINDS]
    if humans and last and "under" not in str(last):
        raise ValidationError("Please upload %s." % " or ".join(humans))
    raise last or ValidationError("Please choose a file first.")


def safe_filename(name):
    base = os.path.basename(name or "file")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:120]


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class _LocalBackend:
    """Stores bytes under var/uploads. Files are served only through an
    authenticated route, never as static assets."""

    def save(self, storage_key, data):
        path = self.path_for(storage_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return storage_key

    def path_for(self, storage_key):
        root = os.path.abspath(config.UPLOAD_DIR)
        path = os.path.abspath(os.path.join(root, storage_key))
        if not path.startswith(root + os.sep):
            raise ValidationError("Invalid storage key.")
        return path

    def read(self, storage_key):
        with open(self.path_for(storage_key), "rb") as fh:
            return fh.read()

    def exists(self, storage_key):
        return os.path.isfile(self.path_for(storage_key))

    def delete(self, storage_key):
        try:
            os.remove(self.path_for(storage_key))
        except FileNotFoundError:
            pass


# To move to cloud storage, implement the same four methods against boto3 /
# google-cloud-storage and select it with CUEMATH_STORAGE.
_BACKENDS = {"local": _LocalBackend}
backend = _BACKENDS.get(config.STORAGE_BACKEND, _LocalBackend)()


def save(folder, filename, data):
    """Persist bytes and return the storage key."""
    ext = os.path.splitext(filename or "")[1].lower()[:10]
    key = "%s/%s%s" % (folder.strip("/"), uuid.uuid4().hex, ext)
    return backend.save(key, data)


def read(storage_key):
    return backend.read(storage_key)


def local_path(storage_key):
    return backend.path_for(storage_key)


def exists(storage_key):
    return backend.exists(storage_key)


def delete(storage_key):
    backend.delete(storage_key)


def human_size(num_bytes):
    num = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return "%d %s" % (num, unit) if unit == "B" else "%.1f %s" % (num, unit)
        num /= 1024
