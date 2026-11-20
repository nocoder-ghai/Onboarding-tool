"""Machine-to-machine API for the Training ATS (coach-training).

When a coach graduates from training, the ATS sends them here so they can sign
in straight away, instead of waiting for an admin to bulk-create their account
from a CSV. Before this existed, a graduate who followed the "Start onboarding"
link before that upload had happened was told "We couldn't match those details."

One endpoint, CREATE-ONLY:

    POST /api/ats/tutors
    Authorization: Bearer <ATS_SYNC_TOKEN>
    Content-Type: application/json
    {"name": "...", "email": "...", "phone": "...",
     "db_id": "...", "grade_cohort_key": "k_5" | "g3_8" | "g9_12"}

It never changes an account that already exists. A coach an admin created by
hand, or one who has already started onboarding, keeps exactly what they have.

Answers (always JSON, never the HTML error page):

    201 {"result": "created", "user_id": ..., "matched_on": null}
    200 {"result": "exists",  "user_id": ..., "matched_on": "db_id" | "email",
         "is_active": true | false}
    409 {"result": "conflict", "reason": "..."}   another account holds a value
    400 {"result": "invalid",  "reason": "..."}   the payload cannot be used
    401 unauthorised; 503 when ATS_SYNC_TOKEN is not set on this deployment.

A new account is created exactly the way /admin/tutors/bulk-create creates one:
the same password rule, the same Orientation invite, the same progress sync.
"""

import hmac
import os
import re

from . import audit, content, db, progress, security
from .micro import json_response
from .util import wrap

# The ATS's GradeCohort enum is mapped to these keys on its side; keys, not
# names, because an admin can rename a cohort here and a key never changes.
_GRADE_COHORT_KEYS = {"k_5", "g3_8", "g9_12"}


def _token():
    return (os.environ.get("ATS_SYNC_TOKEN") or "").strip()


def _authorised(request):
    expected = _token()
    header = request.headers.get("Authorization", "") or ""
    if not header.startswith("Bearer "):
        return False
    supplied = header[len("Bearer "):].strip()
    return bool(supplied) and hmac.compare_digest(supplied.encode(),
                                                  expected.encode())


def _find_existing(email, phone, db_id):
    """Every account that already holds one of these values, by field."""
    by_db_id = db.one("SELECT * FROM users WHERE db_id = ?", (db_id,)) if db_id else None
    by_email = db.one("SELECT * FROM users WHERE email = ?", (email,)) if email else None
    by_phone = db.one("SELECT * FROM users WHERE phone = ?", (phone,)) if phone else None
    return by_db_id, by_email, by_phone


def _decide(by_db_id, by_email, by_phone):
    """Create, report an existing account, or refuse — reading only.

    The coach is the account that carries their db_id, or failing that their
    email. A phone held by a DIFFERENT account is a conflict rather than a
    match: phone is unique here, so creating would fail, and calling it a
    match would report a coach as able to sign in with somebody else's
    account."""
    if by_db_id is not None and by_email is not None and by_db_id["id"] != by_email["id"]:
        return ("conflict", "db_id and email belong to two different accounts")
    if by_db_id is not None:
        return ("exists", by_db_id, "db_id")
    if by_email is not None:
        return ("exists", by_email, "email")
    if by_phone is not None:
        return ("conflict", "phone number already belongs to another account")
    return ("create",)


def _exists_response(user, matched_on):
    return json_response({"result": "exists", "user_id": user["id"],
                          "matched_on": matched_on,
                          "is_active": bool(user["is_active"])}, status=200)


def register(app):

    @app.route("/api/ats/tutors", methods=["POST"])
    def ats_create_tutor(request):
        if not _token():
            return json_response({"result": "unavailable",
                                  "reason": "ATS_SYNC_TOKEN is not set"}, status=503)
        if not _authorised(request):
            return json_response({"result": "unauthorised"}, status=401)
        if not request.headers.get("Content-Type", "").startswith("application/json"):
            return json_response({"result": "invalid",
                                  "reason": "send application/json"}, status=400)

        name = (request.get("name", "") or "").strip()
        email = security.normalise_email(request.get("email", "") or "")
        phone = security.normalise_phone(request.get("phone", "") or "")
        db_id = (request.get("db_id", "") or "").strip().lower()
        grade_key = (request.get("grade_cohort_key", "") or "").strip()

        # The same checks bulk-create applies, in the same order.
        if not name:
            return json_response({"result": "invalid", "reason": "missing name"}, status=400)
        if not email or not security.EMAIL_RE.match(email):
            return json_response({"result": "invalid",
                                  "reason": "missing or invalid email"}, status=400)
        if not phone:
            return json_response({"result": "invalid", "reason": "missing phone"}, status=400)
        if len(re.sub(r"\D", "", phone)) < 8:
            return json_response({"result": "invalid",
                                  "reason": "phone number looks invalid"}, status=400)

        decision = _decide(*_find_existing(email, phone, db_id))
        if decision[0] == "conflict":
            return json_response({"result": "conflict", "reason": decision[1]}, status=409)
        if decision[0] == "exists":
            return _exists_response(decision[1], decision[2])

        grade_cohort_id = None
        if grade_key in _GRADE_COHORT_KEYS:
            row = db.one("SELECT id FROM grade_cohorts WHERE key = ?", (grade_key,))
            grade_cohort_id = row["id"] if row else None

        try:
            user_id = db.insert("users", {
                "name": name, "email": email, "phone": phone,
                "password_hash": security.hash_password(
                    security.generate_password(phone)),
                "role_key": "tutor", "region_id": None,
                "grade_cohort_id": grade_cohort_id, "db_id": db_id or None,
                "created_at": db.now(), "last_activity_at": db.now(),
            })
        except Exception:
            # Two pushes for the same coach can race; the unique indexes
            # decide the winner. Re-read and answer as if we had arrived second.
            decision = _decide(*_find_existing(email, phone, db_id))
            if decision[0] == "exists":
                return _exists_response(decision[1], decision[2])
            if decision[0] == "conflict":
                return json_response({"result": "conflict", "reason": decision[1]},
                                     status=409)
            raise

        user = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        session = progress.orientation_session_for(user)
        if session is not None:
            progress.invite_to_orientation(user, session["id"])
        progress.sync(user, notifications=False)
        audit.record(None, "tutor.ats_create", "user", user_id,
                     "Created from the Training ATS (%s)" % (email,))
        return json_response({"result": "created", "user_id": user_id,
                              "matched_on": None,
                              "grade_cohort": (content.grade_cohort_name(grade_cohort_id)
                                               if grade_cohort_id else None)},
                             status=201)
