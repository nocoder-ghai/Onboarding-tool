"""Runtime configuration. Everything is overridable by environment variable."""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BASE_DIR, "app")
VAR_DIR = os.environ.get("CUEMATH_VAR_DIR", os.path.join(BASE_DIR, "var"))

DB_PATH = os.environ.get("CUEMATH_DB", os.path.join(VAR_DIR, "onboarding.db"))
SCHEMA_PATH = os.path.join(APP_DIR, "schema.sql")
TEMPLATE_DIR = os.path.join(APP_DIR, "templates")
STATIC_DIR = os.path.join(APP_DIR, "static")

# Local object store. Swap STORAGE_BACKEND to 's3'/'gcs' in storage.py to move
# uploads to cloud object storage without touching the rest of the app.
STORAGE_BACKEND = os.environ.get("CUEMATH_STORAGE", "local")
UPLOAD_DIR = os.environ.get("CUEMATH_UPLOAD_DIR", os.path.join(VAR_DIR, "uploads"))

HOST = os.environ.get("CUEMATH_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUEMATH_PORT", "8000"))

# Signing key for session cookies. Generated once into var/secret.key on first
# run; set CUEMATH_SECRET in production so it is identical across instances.
SECRET_ENV = os.environ.get("CUEMATH_SECRET")
SECRET_PATH = os.path.join(VAR_DIR, "secret.key")

SESSION_MAX_AGE = int(os.environ.get("CUEMATH_SESSION_MAX_AGE", 60 * 60 * 24 * 14))
OTP_TTL_SECONDS = 10 * 60
OTP_MAX_ATTEMPTS = 5

MAX_IMAGE_MB = int(os.environ.get("CUEMATH_MAX_IMAGE_MB", "8"))
MAX_VIDEO_MB = int(os.environ.get("CUEMATH_MAX_VIDEO_MB", "60"))
MAX_DOC_MB = int(os.environ.get("CUEMATH_MAX_DOC_MB", "25"))

# In dev the OTP is printed to the console so you can log in without email/SMS.
PRINT_OTP = os.environ.get("CUEMATH_PRINT_OTP", "1") == "1"


def ensure_dirs():
    for path in (VAR_DIR, UPLOAD_DIR):
        os.makedirs(path, exist_ok=True)


def get_secret():
    if SECRET_ENV:
        return SECRET_ENV.encode()
    ensure_dirs()
    if not os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "wb") as fh:
            fh.write(os.urandom(48))
        os.chmod(SECRET_PATH, 0o600)
    with open(SECRET_PATH, "rb") as fh:
        return fh.read()
