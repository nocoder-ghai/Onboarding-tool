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
