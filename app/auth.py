"""Session loading and role guards.

Admin access is decided by the role's `can_admin` flag in the database — not by
the URL — so a tutor who guesses /admin gets a 403, and a read-only "viewer"
admin can browse every admin page but cannot POST.
"""

import functools
import urllib.parse

from . import db
from .micro import HttpError, redirect
from .util import wrap


def load_user(request):
    """`before` hook: attach the signed-in user (or None) to the request."""
    user_id = request.session.get("uid")
    request.user = None
    request.role = None
    if not user_id:
        return None
    row = db.one("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,))
    if row is None:
        request.session_clear()
        return None
    request.user = wrap(row)
    request.role = wrap(db.one("SELECT * FROM roles WHERE key = ?",
                               (row["role_key"],)))
    return None


def sign_in(request, user):
    request.session_clear()
    request.session_set("uid", user["id"])
    db.execute("UPDATE users SET last_login_at = ?, last_activity_at = ? WHERE id = ?",
               (db.now(), db.now(), user["id"]))
    request.user = wrap(db.one("SELECT * FROM users WHERE id = ?", (user["id"],)))


def sign_out(request):
    request.session_clear()
    request.user = None


def is_admin(request):
    return bool(request.role and request.role["can_admin"])


def can_write(request):
    return bool(request.role and request.role["can_write"])


def can_write_tutor(request, tutor_captain_id):
    """Full admins may write onboarding data for any tutor. A viewer
    ("Captain") may only write it for a tutor assigned to them — used for
    class review and compliance logging, which the read-only viewer role
    is otherwise blocked from doing."""
    if can_write(request):
        return True
    return bool(request.role and request.role["key"] == "viewer"
                and request.user and tutor_captain_id == request.user["id"])


def home_for(user):
    if user is None:
        return "/login"
    row = db.one("SELECT can_admin FROM roles WHERE key = ?", (user["role_key"],))
    return "/admin" if (row and row["can_admin"]) else "/dashboard"


# --------------------------------------------------------------------------- #
# Decorators
# --------------------------------------------------------------------------- #

def tutor_required(fn):
    @functools.wraps(fn)
    def inner(request, **kwargs):
        if request.user is None:
            nxt = urllib.parse.quote(request.path)
            return redirect("/login?next=%s" % nxt)
        if request.user["role_key"] != "tutor":
            # Admins don't have a journey of their own.
            return redirect("/admin")
        return fn(request, **kwargs)
    return inner


def admin_required(fn):
    @functools.wraps(fn)
    def inner(request, **kwargs):
        if request.user is None:
            nxt = urllib.parse.quote(request.path)
            return redirect("/admin/login?next=%s" % nxt)
        if not is_admin(request):
            raise HttpError(403, "You don't have access to the admin panel.")
        return fn(request, **kwargs)
    return inner


def admin_write_required(fn):
    """POST-only admin actions: blocked for the read-only viewer role."""
    @functools.wraps(fn)
    def inner(request, **kwargs):
        if request.user is None:
            return redirect("/admin/login")
        if not is_admin(request):
            raise HttpError(403, "You don't have access to the admin panel.")
        if not can_write(request):
            raise HttpError(403, "Your account has read-only access.")
        request.verify_csrf()
        return fn(request, **kwargs)
    return inner


def captain_write_required(fn):
    """POST actions a Captain may take, but only for their own assigned
    tutors (class review, compliance logging) — full admins can act on any
    tutor. `fn` must take `user_id` as its first positional argument after
    `request`, naming the tutor being acted on."""
    @functools.wraps(fn)
    def inner(request, user_id, **kwargs):
        if request.user is None:
            return redirect("/admin/login")
        if not is_admin(request):
            raise HttpError(403, "You don't have access to the admin panel.")
        tutor = db.one("SELECT captain_id FROM users WHERE id = ? "
                       "AND role_key = 'tutor'", (user_id,))
        if tutor is None:
            raise HttpError(404, "Unknown tutor.")
        if not can_write_tutor(request, tutor["captain_id"]):
            raise HttpError(403, "That tutor isn't assigned to you.")
        request.verify_csrf()
        return fn(request, user_id, **kwargs)
    return inner


def tutor_post(fn):
    """Tutor POST actions: authenticated plus CSRF-checked."""
    @functools.wraps(fn)
    def inner(request, **kwargs):
        if request.user is None:
            return redirect("/login")
        if request.user["role_key"] != "tutor":
            raise HttpError(403, "Tutor accounts only.")
        request.verify_csrf()
        return fn(request, **kwargs)
    return inner
