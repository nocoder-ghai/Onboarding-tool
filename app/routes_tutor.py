"""Tutor-facing screens: dashboard, stage detail, policies, uploads, certificate."""

from . import audit, content, db, notify, progress, storage, util
from .auth import tutor_post, tutor_required
from .micro import HttpError, Response, file_response, redirect
from .util import wrap


def register(app):

    # ------------------------------------------------------------ dashboard #
    @app.route("/dashboard")
    @tutor_required
    def dashboard(request):
        user = request.user
        states = progress.sync(user)
        summary = progress.overall(states)
        progress.touch_activity(user["id"])
        class_stage = content.stage_by_key("class_and_app_training")
        welcome_video = content.document_by_key("nancy_welcome_video")
        if welcome_video:
            welcome_video.current = content.current_version(welcome_video.id)
        return app.render(
            request, "tutor/dashboard.html",
            states=states, summary=summary,
            class_slot=progress.class_slot_for(user), class_stage=class_stage,
            links=content.global_links(user["region_id"], user["grade_cohort_id"]),
            intro=db.setting("dashboard_intro", ""),
            unread=notify.unread_count(user["id"]),
            welcome_video=welcome_video)

    # ---------------------------------------------------------- stage detail #
    @app.route("/stage/<int:stage_id>")
    @tutor_required
    def stage_detail(request, stage_id):
        user = request.user
        states = progress.sync(user)
        state = progress.stage_detail(user, stage_id, states)
        if state is None:
            raise HttpError(404, "That stage isn't part of your journey.")
        progress.touch_activity(user["id"])
        idx = next((i for i, s in enumerate(states) if s.id == stage_id), None)
        next_stage = (states[idx + 1] if idx is not None and idx + 1 < len(states)
                      else None)
        return app.render(request, "tutor/stage.html", stage=state, states=states,
                          summary=progress.overall(states), next_stage=next_stage)

    # -------------------------------------------------------------- learnosity #
    @app.route("/learnosity")
    @tutor_required
    def learnosity(request):
        user = request.user
        states = progress.sync(user, notifications=False)
        comp = wrap(db.one("SELECT * FROM components WHERE key = 'learnosity'"))
        links = content.links_for("component", comp.id, user["region_id"],
                                  user["grade_cohort_id"]) if comp else []
        return app.render(request, "tutor/learnosity.html", states=states,
                          summary=progress.overall(states), comp=comp, links=links)

    # ------------------------------------------------------- item: tick off #
    @app.route("/item/<int:item_id>/toggle", methods=["POST"])
    @tutor_post
    def toggle_item(request, item_id):
        item = content.sub_item(item_id)
        if item is None:
            raise HttpError(404, "Unknown step.")
        back = request.get("back", "/dashboard")
        try:
            progress.toggle_sub_item(request.user, item, request.checked("done"))
        except (progress.ValidationError, progress.LockedError) as exc:
            request.flash(str(exc), "error")
            return redirect(back)
        request.flash("Nice — “%s” is ticked off." % item.title
                      if request.checked("done")
                      else "Reopened “%s”." % item.title, "ok")
        return redirect(back)

    # ---------------------------------------------------- video: mark watched #
    @app.route("/document/<int:document_id>/watched", methods=["POST"])
    @tutor_post
    def video_watched(request, document_id):
        progress.mark_video_watched(request.user["id"], document_id)
        return Response("ok")

    # --------------------------------------------------- policy: read & ack #
    @app.route("/policy/<int:item_id>")
    @tutor_required
    def policy_view(request, item_id):
        user = request.user
        item = content.sub_item(item_id)
        if item is None or item.kind != "policy":
            raise HttpError(404, "Unknown policy.")
        try:
            stage = progress.assert_item_actionable(user, item)
        except (progress.ValidationError, progress.LockedError) as exc:
            request.flash(str(exc), "error")
            return redirect("/dashboard")
        doc = content.primary_document(item.id, user["region_id"],
                                       user["grade_cohort_id"])
        version = doc.current if doc else None
        ack = wrap(db.one(
            "SELECT * FROM policy_acknowledgements WHERE user_id = ? "
            "AND sub_item_id = ? AND document_version_id = ?",
            (user["id"], item.id, version.id if version else None)))
        return app.render(request, "tutor/policy.html", item=item, stage=stage,
                          document=doc, version=version, ack=ack,
                          questions=content.quiz_questions(item.id),
                          links=content.links_for("sub_item", item.id,
                                                  user["region_id"],
                                                  user["grade_cohort_id"]))

    @app.route("/policy/<int:item_id>/quiz", methods=["POST"])
    @tutor_post
    def policy_quiz(request, item_id):
        item = content.sub_item(item_id)
        if item is None or item.kind != "policy":
            raise HttpError(404, "Unknown policy.")
        questions = content.quiz_questions(item_id)
        answers = {str(q.id): request.get("q_%d" % q.id, "") for q in questions}
        try:
            passed, score, total = progress.submit_quiz(request.user, item, answers)
        except (progress.ValidationError, progress.LockedError) as exc:
            request.flash(str(exc), "error")
            return redirect("/policy/%d" % item_id)
        if passed:
            request.flash("Nailed it — “%s” is complete (%d/%d)."
                          % (item.title, score, total), "ok")
        else:
            request.flash("%d/%d correct — have another look and try again."
                          % (score, total), "error")
        return redirect("/policy/%d" % item_id)

    @app.route("/policy/<int:item_id>/acknowledge", methods=["POST"])
    @tutor_post
    def policy_ack(request, item_id):
        item = content.sub_item(item_id)
        if item is None:
            raise HttpError(404, "Unknown policy.")
        back = request.get("back", "/policy/%d" % item_id)
        if not request.checked("confirm"):
            request.flash("Tick the confirmation box so we know you've read it.",
                          "error")
            return redirect(back)
        try:
            progress.acknowledge_policy(request.user, item, request.client_ip)
        except (progress.ValidationError, progress.LockedError) as exc:
            request.flash(str(exc), "error")
            return redirect(back)
        stage = progress.stage_for_sub_item(item.id)
        request.flash("Thanks for reading “%s” — acknowledged." % item.title, "ok")
        return redirect(("/stage/%d" % stage.id) if stage else "/dashboard")

    # ----------------------------------------------------------- item: file #
    @app.route("/item/<int:item_id>/upload", methods=["POST"])
    @tutor_post
    def upload_item(request, item_id):
        item = content.sub_item(item_id)
        if item is None:
            raise HttpError(404, "Unknown step.")
        back = request.get("back", "/dashboard")
        upload = request.file("file")
        try:
            status = progress.submit_upload(request.user, item, upload)
        except (progress.ValidationError, progress.LockedError,
                storage.ValidationError) as exc:
            request.flash(str(exc), "error")
            return redirect(back)
        if status == "submitted":
            request.flash("Got it — “%s” is with your mentor for a quick look."
                          % item.title, "ok")
        else:
            request.flash("Uploaded — “%s” is done." % item.title, "ok")
        return redirect(back)

    # ------------------------------------------------------ component: tick #
    @app.route("/component/<int:component_id>/complete", methods=["POST"])
    @tutor_post
    def complete_component(request, component_id):
        comp = content.component(component_id)
        if comp is None:
            raise HttpError(404, "Unknown training component.")
        back = request.get("back", "/stage/%d" % comp.stage_id)
        try:
            progress.mark_component(request.user, comp, request.checked("done"))
        except (progress.ValidationError, progress.LockedError) as exc:
            request.flash(str(exc), "error")
            return redirect(back)
        request.flash("“%s” marked complete." % comp.title if request.checked("done")
                      else "“%s” reopened." % comp.title, "ok")
        return redirect(back)

    # -------------------------------------------------------- notifications #
    @app.route("/notifications")
    @tutor_required
    def notifications(request):
        items = notify.for_user(request.user["id"])
        notify.mark_all_read(request.user["id"])
        return app.render(request, "tutor/notifications.html", items=items)

    # ----------------------------------------------------------- completion #
    @app.route("/complete")
    @tutor_required
    def completion(request):
        user = request.user
        states = progress.sync(user)
        summary = progress.overall(states)
        if not summary.all_complete:
            request.flash("Almost there — finish every stage to unlock your "
                          "certificate.", "error")
            return redirect("/dashboard")
        fresh = wrap(db.one("SELECT * FROM users WHERE id = ?", (user["id"],)))
        return app.render(request, "tutor/complete.html", states=states,
                          summary=summary, tutor=fresh,
                          region=content.region(user["region_id"]),
                          grade_cohort=content.grade_cohort(user["grade_cohort_id"]),
                          body=db.setting("certificate_body", ""),
                          signatory=db.setting("certificate_signatory", ""))

    # ------------------------------------------------ class-with-a-student #
    @app.route("/class-slot/<int:slot_id>/book", methods=["POST"])
    @tutor_post
    def book_class_slot(request, slot_id):
        back = request.get("back", "/dashboard")
        try:
            progress.book_class_slot(request.user, slot_id)
        except progress.ValidationError as exc:
            request.flash(str(exc), "error")
            return redirect(back)
        request.flash("Booked — check the prep tips before your class.", "ok")
        return redirect(back)

    @app.route("/class-slot/<int:slot_id>/release", methods=["POST"])
    @tutor_post
    def release_class_slot(request, slot_id):
        back = request.get("back", "/dashboard")
        try:
            progress.release_class_slot(request.user, slot_id)
        except progress.ValidationError as exc:
            request.flash(str(exc), "error")
            return redirect(back)
        request.flash("Slot released — pick another time whenever you're ready.", "ok")
        return redirect(back)

    @app.route("/class-slot/<int:slot_id>/calendar.ics")
    @tutor_required
    def class_slot_ics(request, slot_id):
        slot = wrap(db.one("SELECT * FROM class_slots WHERE id = ? AND tutor_id = ?",
                           (slot_id, request.user["id"])))
        if slot is None:
            raise HttpError(404, "Unknown class.")
        body = util.ics_event(
            "class-slot-%d@cuemath-onboarding" % slot.id,
            "Cuemath: class with %s" % (slot.student_name or "a student"),
            slot.notes or "Your real-life class experience for coach onboarding.",
            slot.starts_at, slot.duration_minutes)
        return Response(body, content_type="text/calendar; charset=utf-8",
                        headers=[("Content-Disposition",
                                  'attachment; filename="cuemath-class.ics"')])

    # -------------------------------------------------------------- profile #
    @app.route("/profile", methods=["GET", "POST"])
    @tutor_required
    def profile(request):
        user = request.user
        if request.method == "POST":
            request.verify_csrf()
            from . import security
            name = request.get("name", "").strip()
            values = {}
            if len(name) >= 2 and name != user["name"]:
                values["name"] = name
            new_password = request.get("password", "")
            if new_password:
                problem = security.password_problem(new_password)
                if problem:
                    request.flash(problem, "error")
                    return redirect("/profile")
                values["password_hash"] = security.hash_password(new_password)
            if values:
                db.update("users", user["id"], values)
                audit.record(request, "tutor.profile_update", "user", user["id"],
                             "Updated own profile")
                request.flash("Saved.", "ok")
            return redirect("/profile")
        return app.render(request, "tutor/profile.html",
                          region=content.region(user["region_id"]),
                          grade_cohort=content.grade_cohort(user["grade_cohort_id"]),
                          acks=progress.acknowledgements_for(user["id"]),
                          submissions=progress.submissions_for(user["id"]))

    # ---------------------------------------------------------- file access #
    @app.route("/file/document/<int:version_id>")
    def serve_document(request, version_id):
        """Any signed-in user may read a document version; nobody else can."""
        if request.user is None:
            return redirect("/login?next=/dashboard")
        row = db.one(
            "SELECT dv.*, d.title AS doc_title FROM document_versions dv "
            "JOIN documents d ON d.id = dv.document_id WHERE dv.id = ?",
            (version_id,))
        if row is None or not storage.exists(row["storage_key"]):
            raise HttpError(404, "That file is no longer available.")
        return file_response(storage.local_path(row["storage_key"]),
                             download_name=row["filename"])

    @app.route("/file/document/<int:version_id>/thumbnail")
    def serve_document_thumbnail(request, version_id):
        """A preview frame for a video document. 404s (broken image, caught by
        the template's onerror) if the file's missing or isn't a video."""
        if request.user is None:
            raise HttpError(404, "Not found.")
        row = db.one(
            "SELECT dv.storage_key, d.kind AS doc_kind FROM document_versions dv "
            "JOIN documents d ON d.id = dv.document_id WHERE dv.id = ?",
            (version_id,))
        if row is None or row["doc_kind"] != "video" \
                or not storage.exists(row["storage_key"]):
            raise HttpError(404, "No thumbnail available.")
        thumb_key = storage.thumbnail_for_video(row["storage_key"])
        if thumb_key is None:
            raise HttpError(404, "No thumbnail available.")
        return file_response(storage.local_path(thumb_key),
                             download_name="thumbnail.png")

    @app.route("/file/submission/<int:submission_id>")
    def serve_submission(request, submission_id):
        """A tutor sees only their own uploads; admins see everyone's."""
        if request.user is None:
            return redirect("/login")
        row = db.one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
        if row is None:
            raise HttpError(404, "Not found.")
        from .auth import is_admin
        if row["user_id"] != request.user["id"] and not is_admin(request):
            raise HttpError(403, "That isn't yours to open.")
        if not storage.exists(row["storage_key"]):
            raise HttpError(404, "That file is no longer available.")
        return file_response(storage.local_path(row["storage_key"]),
                             download_name=row["filename"])
