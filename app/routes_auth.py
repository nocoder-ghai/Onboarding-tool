"""Login, signup, OTP and logout for both tutors and admins."""

import urllib.parse

from . import audit, auth, content, db, notify, progress, security
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
    @app.route("/signup", methods=["GET", "POST"])
    def signup(request):
        regions = content.regions()
        grade_cohorts = content.grade_cohorts()
        if request.user is not None:
            return redirect(auth.home_for(request.user))
        form = {"name": "", "email": "", "phone": "", "region_id": "",
                "grade_cohort_id": ""}
        if request.method == "GET":
            return app.render(request, "auth/signup.html", regions=regions,
                              grade_cohorts=grade_cohorts, form=form,
                              welcome=db.setting("signup_intro", ""))

        request.verify_csrf()
        form = {
            "name": request.get("name", "").strip(),
            "email": security.normalise_email(request.get("email", "")),
            "phone": security.normalise_phone(request.get("phone", "")),
            "region_id": request.get("region_id", ""),
            "grade_cohort_id": request.get("grade_cohort_id", ""),
        }
        password = request.get("password", "")
        errors = []
        if len(form["name"]) < 2:
            errors.append("Please tell us your full name.")
        if not form["email"] and not form["phone"]:
            errors.append("Give us either an email address or a mobile number.")
        if form["email"] and not security.EMAIL_RE.match(form["email"]):
            errors.append("That email address doesn't look right.")
        region_id = request.get_int("region_id")
        if regions and not region_id:
            errors.append("Choose the region you'll be teaching in.")
        grade_cohort_id = request.get_int("grade_cohort_id")
        if grade_cohorts and not grade_cohort_id:
            errors.append("Choose the grade cohort you'll be teaching.")
        if password:
            problem = security.password_problem(password)
            if problem:
                errors.append(problem)
        if form["email"] and db.one("SELECT 1 FROM users WHERE email = ?",
                                    (form["email"],)):
            errors.append("An account already exists with that email. Try signing in.")
        if form["phone"] and db.one("SELECT 1 FROM users WHERE phone = ?",
                                    (form["phone"],)):
            errors.append("An account already exists with that number. Try signing in.")
        if errors:
            for message in errors:
                request.flash(message, "error")
            return app.render(request, "auth/signup.html", regions=regions,
                              grade_cohorts=grade_cohorts, form=form,
                              welcome=db.setting("signup_intro", ""))

        user_id = db.insert("users", {
            "name": form["name"],
            "email": form["email"] or None,
            "phone": form["phone"] or None,
            "password_hash": security.hash_password(password) if password else None,
            "role_key": "tutor",
            "region_id": region_id,
            "grade_cohort_id": grade_cohort_id,
            "created_at": db.now(),
            "last_activity_at": db.now(),
        })
        user = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        auth.sign_in(request, user)
        audit.record(request, "auth.signup", "user", user_id,
                     "New tutor account: %s" % form["name"])

        # Day 1: open Orientation and send the invite.
        session = progress.orientation_session_for(user)
        if session is not None:
            progress.invite_to_orientation(user, session["id"])
        progress.sync(user)
        request.flash("Welcome to Cuemath, %s! Let's get you started."
                      % form["name"].split(" ")[0], "ok")
        return redirect("/dashboard")

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
