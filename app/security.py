"""Password hashing, OTP issuance/verification and identifier normalisation."""

import base64
import datetime
import hashlib
import hmac
import os
import re
import secrets

from . import config, db

PBKDF2_ROUNDS = 200_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password, stored):
    if not stored or not password:
        return False
    try:
        algo, rounds, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     base64.b64decode(salt_b64), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def password_problem(password):
    """Return a human message if the password is too weak, else None."""
    if len(password or "") < 8:
        return "Password must be at least 8 characters."
    if password.isdigit() or password.isalpha():
        return "Mix letters and numbers so your password is harder to guess."
    return None


def generate_password(phone):
    """Deterministic password for a bulk-created account: a letter prefix plus
    the tutor's own phone digits, so it's easy for them to remember and still
    clears password_problem's letters+numbers check."""
    digits = re.sub(r"\D", "", phone or "")
    return "Cue@%s" % digits


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #

def normalise_email(value):
    return (value or "").strip().lower()


def normalise_phone(value):
    """Keep digits and a leading +, so '+91 98765 43210' matches '+919876543210'."""
    raw = (value or "").strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" if plus else "") + digits


def classify_identifier(value):
    """Return ('email'|'phone'|None, normalised)."""
    raw = (value or "").strip()
    if not raw:
        return None, ""
    if "@" in raw:
        email = normalise_email(raw)
        return ("email", email) if EMAIL_RE.match(email) else (None, email)
    phone = normalise_phone(raw)
    return ("phone", phone) if len(re.sub(r"\D", "", phone)) >= 8 else (None, phone)


def find_user_by_identifier(value):
    kind, norm = classify_identifier(value)
    if kind == "email":
        return db.one("SELECT * FROM users WHERE email = ?", (norm,))
    if kind == "phone":
        return db.one("SELECT * FROM users WHERE phone = ?", (norm,))
    return None


# --------------------------------------------------------------------------- #
# One-time passcodes
# --------------------------------------------------------------------------- #

def _hash_code(identifier, code):
    return hashlib.sha256(("%s|%s" % (identifier, code)).encode()).hexdigest()


def issue_otp(identifier):
    """Create a 6-digit code, invalidating any previous unused ones."""
    code = "%06d" % secrets.randbelow(1_000_000)
    expires = (datetime.datetime.utcnow()
               + datetime.timedelta(seconds=config.OTP_TTL_SECONDS))
    db.execute("UPDATE otp_codes SET consumed_at = ? "
               "WHERE identifier = ? AND consumed_at IS NULL",
               (db.now(), identifier))
    db.insert("otp_codes", {
        "identifier": identifier,
        "code_hash": _hash_code(identifier, code),
        "created_at": db.now(),
        "expires_at": expires.replace(microsecond=0).isoformat(sep=" "),
    })
    return code


def verify_otp(identifier, code):
    """Return (ok, message). Consumes the code on success."""
    row = db.one(
        "SELECT * FROM otp_codes WHERE identifier = ? AND consumed_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (identifier,))
    if row is None:
        return False, "That code has already been used. Please request a new one."
    if db.parse_ts(row["expires_at"]) < datetime.datetime.utcnow():
        return False, "That code has expired. Please request a new one."
    if row["attempts"] >= config.OTP_MAX_ATTEMPTS:
        db.execute("UPDATE otp_codes SET consumed_at = ? WHERE id = ?",
                   (db.now(), row["id"]))
        return False, "Too many attempts. Please request a new code."
    if not hmac.compare_digest(row["code_hash"],
                               _hash_code(identifier, (code or "").strip())):
        db.execute("UPDATE otp_codes SET attempts = attempts + 1 WHERE id = ?",
                   (row["id"],))
        return False, "That code doesn't match. Please try again."
    db.execute("UPDATE otp_codes SET consumed_at = ? WHERE id = ?",
               (db.now(), row["id"]))
    return True, ""
