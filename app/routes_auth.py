"""Login, signup, OTP and logout for both tutors and admins."""

import urllib.parse

from . import audit, auth, db, notify, progress, security
from .micro import redirect
from .util import wrap


def _safe_next(raw, fallback):
    """Only allow same-site relative redirects."""
    target = urllib.parse.unquote(raw or "")
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def register(app):

    # ---------------------------------------------------------------- login #
    @app.route("/login", methods=["GET", "POST"])
    def login(request):
        nxt = request.get("next", "")
        if request.user is not None:
            return redirect(_safe_next(nxt, auth.home_for(request.user)))
        if request.method == "GET":
            return app.render(request, "auth/login.html", next=nxt,
                              identifier="", admin_mode=False)

        request.verify_csrf()
        identifier = request.get("identifier", "").strip()
        kind, norm = security.classify_identifier(identifier)
        if kind is None:
            request.flash("Enter a valid email address or mobile number.", "error")
            return app.render(request, "auth/login.html", next=nxt,
                              identifier=identifier, admin_mode=False)

        user = security.find_user_by_identifier(identifier)
        action = request.get("action", "password")

        if action == "otp":
            # Don't reveal whether an account exists; always show the code screen.
            if user is not None and user["is_active"]:
                code = security.issue_otp(norm)
                notify.send_otp(norm, code, kind)
            request.session_set("otp_identifier", norm)
            request.session_set("otp_kind", kind)
            return redirect("/login/otp?next=%s" % urllib.parse.quote(nxt))

        password = request.get("password", "")
        if user is None or not user["is_active"] or \
                not security.verify_password(password, user["password_hash"]):
            request.flash("We couldn't match those details. Please try again.",
                          "error")
            return app.render(request, "auth/login.html", next=nxt,
                              identifier=identifier, admin_mode=False)

        auth.sign_in(request, user)
        if user["role_key"] == "tutor":
            progress.sync(wrap(user))
        audit.record(request, "auth.login", "user", user["id"], "Signed in")
        return redirect(_safe_next(nxt, auth.home_for(user)))

    @app.route("/login/otp", methods=["GET", "POST"])
    def login_otp(request):
        identifier = request.session.get("otp_identifier", "")
        kind = request.session.get("otp_kind", "email")
        nxt = request.get("next", "")
        if not identifier:
            return redirect("/login")
        if request.method == "GET":
            return app.render(request, "auth/otp.html", identifier=identifier,
                              kind=kind, next=nxt)

        request.verify_csrf()
        if request.get("action") == "resend":
            user = security.find_user_by_identifier(identifier)
            if user is not None and user["is_active"]:
                notify.send_otp(identifier, security.issue_otp(identifier), kind)
            request.flash("We've sent a fresh code.", "ok")
            return redirect("/login/otp?next=%s" % urllib.parse.quote(nxt))

        ok, message = security.verify_otp(identifier, request.get("code", ""))
        if not ok:
            request.flash(message, "error")
            return app.render(request, "auth/otp.html", identifier=identifier,
                              kind=kind, next=nxt)
        user = security.find_user_by_identifier(identifier)
        if user is None or not user["is_active"]:
            request.flash("We couldn't match those details.", "error")
            return redirect("/login")
        auth.sign_in(request, user)
        request.session_pop("otp_identifier")
        request.session_pop("otp_kind")
        if user["role_key"] == "tutor":
            progress.sync(wrap(user))
        audit.record(request, "auth.login_otp", "user", user["id"],
                     "Signed in with a one-time code")
        return redirect(_safe_next(nxt, auth.home_for(user)))

    # --------------------------------------------------------------- signup #
    # Tutor accounts are created by admins (with a fixed password), not via
    # self-signup — keep the route so old links don't 404, but send everyone
    # to /login.
    @app.route("/signup", methods=["GET", "POST"])
    def signup(request):
        return redirect(auth.home_for(request.user) if request.user is not None
                        else "/login")

    # --------------------------------------------------------------- logout #
    @app.route("/logout", methods=["GET", "POST"])
    def logout(request):
        if request.user is not None:
            audit.record(request, "auth.logout", "user", request.user["id"],
                         "Signed out")
        auth.sign_out(request)
        return redirect("/login")

    # ---------------------------------------------------------- admin login #
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login(request):
        nxt = request.get("next", "")
        if request.user is not None and auth.is_admin(request):
            return redirect(_safe_next(nxt, "/admin"))
        if request.method == "GET":
            return app.render(request, "auth/login.html", next=nxt,
                              identifier="", admin_mode=True)

        request.verify_csrf()
        identifier = request.get("identifier", "").strip()
        password = request.get("password", "")
        user = security.find_user_by_identifier(identifier)
        role = None
        if user is not None:
            role = db.one("SELECT * FROM roles WHERE key = ?", (user["role_key"],))
        if user is None or not user["is_active"] or not role or not role["can_admin"] \
                or not security.verify_password(password, user["password_hash"]):
            request.flash("Those admin credentials didn't work.", "error")
            return app.render(request, "auth/login.html", next=nxt,
                              identifier=identifier, admin_mode=True)
        auth.sign_in(request, user)
        audit.record(request, "auth.admin_login", "user", user["id"],
                     "Admin signed in")
        return redirect(_safe_next(nxt, "/admin"))
