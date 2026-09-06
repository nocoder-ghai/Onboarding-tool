"""Application factory: builds the App, wires shared context, registers routes."""

from . import auth, config, content, db, notify, util
from .micro import App, Response


def create_app():
    config.ensure_dirs()
    app = App(template_dir=config.TEMPLATE_DIR,
              static_dir=config.STATIC_DIR,
              secret=config.get_secret())
    app.env.globals.update(util.TEMPLATE_GLOBALS)

    @app.before
    def _load_user(request):
        return auth.load_user(request)

    @app.context
    def _base_context(request):
        user = getattr(request, "user", None)
        ctx = {
            "user": user,
            "role": getattr(request, "role", None),
            "is_admin": auth.is_admin(request),
            "can_write": auth.can_write(request),
            "brand": db.setting("brand_name", "Cuemath"),
            "support_email": db.setting("support_email", ""),
            "nav_unread": 0,
            "director": None,
            "path": request.path,
            "hide_topbar": False,
            "current_stage_id": None,
            "stage_components": None,
            "next_stage": None,
            "icon": "📄",
            "current_item_id": None,
            # _doc_link.html is included for stage- and component-level
            # documents too, where there is no owning step.
            "item": None,
        }
        if user is not None and user["role_key"] == "tutor":
            ctx["nav_unread"] = notify.unread_count(user["id"])
            ctx["region"] = content.region(user["region_id"])
            ctx["mentor_name"] = (content.captain_name(user["captain_id"])
                                  if user["captain_id"] else None)
            ctx["director"] = content.captain(user["captain_id"])
        return ctx

    def _error(request, status, message):
        titles = {403: "Not your door", 404: "We can't find that page",
                  405: "That didn't work", 500: "Something went wrong"}
        try:
            response = app.render(request, "error.html", status=status,
                                  message=message,
                                  headline=titles.get(status, "Something went wrong"))
            response.status = status  # keep the real HTTP status, not 200
            return response
        except Exception:
            return Response("<h1>%d</h1><p>%s</p>" % (status, message),
                            status=status)
    app.error_handler = _error

    from . import routes_auth, routes_tutor, routes_admin
    routes_auth.register(app)
    routes_tutor.register(app)
    routes_admin.register(app)

    @app.route("/")
    def index(request):
        from .micro import redirect
        return redirect(auth.home_for(request.user))

    @app.route("/schedule/<str:token>")
    def shared_schedule(request, token):
        """The class schedule, readable by whoever holds the link — for the
        person booking sessions, who needs the schedule but not the tool.
        Deliberately unauthenticated, so the only thing standing between this
        page and the internet is the secret in the URL."""
        import hmac
        from . import progress
        from .micro import HttpError
        import urllib.parse
        expected = progress.schedule_share_token(create=False)
        if not expected or not hmac.compare_digest(str(token), str(expected)):
            # Say nothing about whether a schedule exists.
            raise HttpError(404, "We can't find that page.")

        every = progress.scheduled_sessions()
        status = request.get("status", "all")
        search = request.get("q", "").strip()

        def in_status(session, wanted):
            if wanted == "booked":
                return session.status == "booked"
            if wanted == "open":
                return session.status == "open"
            if wanted == "upcoming":
                return not session.is_past and session.status != "cancelled"
            if wanted == "past":
                return session.is_past
            return True

        def in_search(session):
            if not search:
                return True
            haystack = " ".join(str(x or "") for x in (
                session.coach_name, session.coach_email, session.student_name,
                session.grade_subject, session.cohort_name, session.region_name))
            return search.lower() in haystack.lower()

        # Counts come from the whole list, so a filter always shows what
        # switching to another one would give you.
        def link(wanted):
            params = {}
            if wanted != "all":
                params["status"] = wanted
            if search:
                params["q"] = search
            query = urllib.parse.urlencode(params)
            return "/schedule/%s%s" % (token, "?" + query if query else "")

        # AttrDict so the template can say f.label — plain dicts have no
        # attribute access in this engine.
        filters = [
            util.AttrDict(
                key=k, label=lbl,
                count=len([s for s in every if in_status(s, k) and in_search(s)]),
                url=link(k), active=(status == k))
            for k, lbl in (("all", "All"), ("upcoming", "Upcoming"),
                           ("booked", "Booked"), ("open", "Not booked"),
                           ("past", "Past"))
        ]
        sessions = [s for s in every if in_status(s, status) and in_search(s)]

        response = app.render(request, "schedule.html",
                              sessions=sessions, filters=filters,
                              search=search, total=len(every),
                              share_path="/schedule/%s" % token,
                              generated_at=db.now(), hide_topbar=True)
        response.headers.append(("X-Robots-Tag", "noindex, nofollow"))
        response.headers.append(("Cache-Control", "no-store"))
        return response

    @app.route("/healthz")
    def healthz(request):
        from .micro import json_response
        return json_response({"status": "ok", "tutors": db.scalar(
            "SELECT COUNT(*) FROM users WHERE role_key='tutor'", (), 0)})

    return app


def bootstrap_check():
    """Return a warning string if the DB isn't initialised/seeded yet."""
    if not db.table_exists("stages"):
        return "Database not initialised. Run: python3 run.py init"
    if not db.scalar("SELECT COUNT(*) FROM stages", (), 0):
        return "No journey content yet. Run: python3 run.py seed"
    return ""
