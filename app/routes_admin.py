"""Admin panel: content management, tutor management, reporting, audit.

Every tutor-visible string, link, document and structural relationship is
editable from here. Write actions require a role with can_write (so the
"viewer" role can read everything and change nothing) and a valid CSRF token —
both enforced by the @writes decorator.
"""

import csv
import datetime
import io
import re

from . import audit, content, db, notify, progress, security, storage, util
from .auth import admin_required, admin_write_required, can_write_tutor, captain_write_required
from .micro import HttpError, Response, file_response, redirect
from .util import wrap, wrap_all

# --------------------------------------------------------------------------- #
# Field collection helpers
# --------------------------------------------------------------------------- #

STAGE_SPEC = [("title", "text"), ("subtitle", "text"), ("description", "text"),
              ("locked_hint", "text"), ("completion_rule", "text"),
              ("is_mandatory", "bool"), ("deadline_days", "int?"),
              ("unlock_after_stage_id", "int?")]

COMPONENT_SPEC = [("title", "text"), ("description", "text"),
                  ("completion_rule", "text"), ("is_mandatory", "bool"),
                  ("region_id", "int?"), ("grade_cohort_id", "int?")]

SUB_ITEM_SPEC = [("title", "text"), ("description", "text"),
                 ("instructions", "text"), ("kind", "text"),
                 ("accept_mime", "text"), ("max_upload_mb", "int?"),
                 ("is_mandatory", "bool"), ("region_id", "int?"),
                 ("grade_cohort_id", "int?"), ("parent_id", "int?")]

LINK_SPEC = [("label", "text"), ("url", "text"), ("description", "text"),
             ("region_id", "int?"), ("grade_cohort_id", "int?"),
             ("is_active", "bool")]

DOC_SPEC = [("title", "text"), ("description", "text"), ("kind", "text"),
            ("region_id", "int?"), ("grade_cohort_id", "int?"),
            ("drive_url", "text"), ("is_active", "bool")]

SESSION_SPEC = [("title", "text"), ("zoom_link", "text"), ("starts_at", "text"),
                ("duration_minutes", "int"), ("host_name", "text"),
                ("notes", "text"), ("region_id", "int?"),
                ("deck_document_id", "int?"), ("is_active", "bool")]

CLASS_SLOT_SPEC = [("starts_at", "text"), ("duration_minutes", "int"),
                   ("student_name", "text"), ("grade_subject", "text"),
                   ("notes", "text"), ("region_id", "int?"),
                   ("grade_cohort_id", "int?")]


def collect(request, spec):
    out = {}
    for name, kind in spec:
        if kind == "text":
            out[name] = request.get(name, "").strip()
        elif kind == "bool":
            out[name] = 1 if request.checked(name) else 0
        elif kind == "int":
            out[name] = request.get_int(name, 0) or 0
        elif kind == "int?":
            out[name] = request.get_int(name)
    return out


def _require(values, field, message):
    if not values.get(field):
        raise HttpError(400, message)


_CLASS_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
                       "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
                       "%b %d, %Y", "%B %d, %Y")
_CLASS_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p")


def _parse_class_datetime(date_text, time_text):
    """Combine loose CSV 'date' and 'time' cells into a starts_at string."""
    date_text = (date_text or "").strip()
    time_text = (time_text or "").strip()
    if not date_text:
        return None
    date_val = None
    for fmt in _CLASS_DATE_FORMATS:
        try:
            date_val = datetime.datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue
    if date_val is None:
        return None
    if not time_text:
        return date_val.isoformat()
    time_val = None
    for fmt in _CLASS_TIME_FORMATS:
        try:
            time_val = datetime.datetime.strptime(time_text.upper(), fmt).time()
            break
        except ValueError:
            continue
    if time_val is None:
        return None
    return "%s %02d:%02d" % (date_val.isoformat(), time_val.hour, time_val.minute)


def _would_cycle(stage_id, prereq_id):
    """Walk the prerequisite chain to keep unlock rules acyclic."""
    seen = set()
    cursor = prereq_id
    while cursor:
        if cursor == stage_id or cursor in seen:
            return True
        seen.add(cursor)
        cursor = db.scalar("SELECT unlock_after_stage_id FROM stages WHERE id = ?",
                           (cursor,))
    return False


def register(app):
    writes = admin_write_required

    def render(request, template, **ctx):
        ctx.setdefault("regions_all", content.regions(active_only=False))
        ctx.setdefault("grade_cohorts_all", content.grade_cohorts(active_only=False))
        return app.render(request, template, **ctx)

    def captain_scope(request):
        """Viewers ('captains') only ever see tutors assigned to them.
        Admins see everyone, unless they explicitly filter by a captain."""
        if request.role and request.role["key"] == "viewer":
            return request.user["id"]
        return request.get_int("captain_id")

    def back_to(request, default):
        target = request.get("back", "") or default
        return redirect(target if target.startswith("/") else default)

    # ==================================================================== #
    # Overview
    # ==================================================================== #

    @app.route("/admin")
    @admin_required
    def admin_home(request):
        captain_id = captain_scope(request)
        is_captain_view = request.role and request.role["key"] == "viewer"
        funnel = progress.funnel(captain_id=captain_id)
        stalled_days = db.setting_int("stalled_days", 7)
        stalled = progress.tutors(stalled_days=stalled_days, captain_id=captain_id)
        recent_sql = "SELECT * FROM users WHERE role_key = 'tutor'"
        recent_args = []
        if captain_id:
            recent_sql += " AND captain_id = ?"
            recent_args.append(captain_id)
        recent_sql += " ORDER BY created_at DESC LIMIT 8"
        recent = wrap_all(db.query(recent_sql, recent_args))
        completed_sql = ("SELECT COUNT(*) FROM users WHERE role_key='tutor' "
                         "AND completed_at IS NOT NULL")
        completed_args = ()
        if captain_id:
            completed_sql += " AND captain_id = ?"
            completed_args = (captain_id,)
        return render(request, "admin/home.html", funnel=funnel,
                      stalled=stalled[:8], stalled_count=len(stalled),
                      stalled_days=stalled_days, recent=recent,
                      is_captain_view=is_captain_view,
                      audit_rows=[] if is_captain_view else audit.recent(8),
                      manager_rollup=(None if is_captain_view else
                                      progress.manager_rollup(stalled_days)),
                      pending_email=db.scalar(
                          "SELECT COUNT(*) FROM email_outbox WHERE sent_at IS NULL",
                          (), 0),
                      completed=db.scalar(completed_sql, completed_args, 0))

    # ==================================================================== #
    # Content: journey tree
    # ==================================================================== #

    @app.route("/admin/content")
    @admin_required
    def admin_content(request):
        show_archived = request.checked("archived")
        stages = content.stages(include_archived=show_archived)
        for stage in stages:
            stage.agenda = content.agenda_items(stage.id,
                                                include_archived=show_archived)
            stage.comps = content.components(stage.id, include_archived=show_archived,
                                             all_regions=True)
            for comp in stage.comps:
                comp.tree = content.sub_item_tree(
                    comp.id, include_archived=show_archived, all_regions=True)
                comp.region_label = content.region_name(comp.region_id)
                comp.grade_cohort_label = content.grade_cohort_name(
                    comp.grade_cohort_id)
        return render(request, "admin/content.html", stages=stages,
                      show_archived=show_archived)

    # ------------------------------------------------------------- stages -- #
    @app.route("/admin/stages/new", methods=["POST"])
    @writes
    def stage_new(request):
        values = collect(request, STAGE_SPEC)
        _require(values, "title", "A stage needs a title.")
        if values["completion_rule"] not in ("components", "admin_marked"):
            values["completion_rule"] = "components"
        values["key"] = util.unique_key("stages", util.slugify(values["title"], "stage"))
        values["sort_order"] = content.next_sort_order("stages", "archived_at IS NULL")
        values["created_at"] = values["updated_at"] = db.now()
        stage_id = db.insert("stages", values)
        audit.record(request, "stage.create", "stage", stage_id,
                     "Created stage “%s”" % values["title"], after=values)
        request.flash("Stage “%s” added." % values["title"], "ok")
        return redirect("/admin/stages/%d" % stage_id)

    @app.route("/admin/stages/<int:stage_id>")
    @admin_required
    def stage_editor(request, stage_id):
        stage = content.stage(stage_id)
        if stage is None:
            raise HttpError(404, "Unknown stage.")
        stage.agenda = content.agenda_items(stage_id, include_archived=True)
        stage.comps = content.components(stage_id, include_archived=True,
                                         all_regions=True)
        for comp in stage.comps:
            comp.region_label = content.region_name(comp.region_id)
            comp.grade_cohort_label = content.grade_cohort_name(comp.grade_cohort_id)
            comp.item_count = db.scalar(
                "SELECT COUNT(*) FROM sub_items WHERE component_id = ? "
                "AND archived_at IS NULL", (comp.id,), 0)
        others = [s for s in content.stages() if s.id != stage_id]
        return render(request, "admin/stage_editor.html", stage=stage,
                      other_stages=others,
                      stage_links=wrap_all(db.query(
                          "SELECT * FROM links WHERE stage_id = ? ORDER BY sort_order",
                          (stage_id,))),
                      documents=content.documents_for("stage", stage_id),
                      history=audit.changes_for("stage", stage_id, 15))

    @app.route("/admin/stages/<int:stage_id>/edit", methods=["POST"])
    @writes
    def stage_edit(request, stage_id):
        stage = content.stage(stage_id)
        if stage is None:
            raise HttpError(404, "Unknown stage.")
        values = collect(request, STAGE_SPEC)
        _require(values, "title", "A stage needs a title.")
        if values["completion_rule"] not in ("components", "admin_marked"):
            values["completion_rule"] = stage.completion_rule
        prereq = values["unlock_after_stage_id"]
        if prereq and _would_cycle(stage_id, prereq):
            request.flash("That unlock rule would create a loop. Not saved.", "error")
            return redirect("/admin/stages/%d" % stage_id)
        values["updated_at"] = db.now()
        db.update("stages", stage_id, values)
        audit.record(request, "stage.update", "stage", stage_id,
                     "Edited stage “%s”" % values["title"],
                     before=dict(stage), after=values)
        request.flash("Stage saved.", "ok")
        return redirect("/admin/stages/%d" % stage_id)

    @app.route("/admin/stages/<int:stage_id>/move", methods=["POST"])
    @writes
    def stage_move(request, stage_id):
        content.move("stages", stage_id, request.get("dir", "up"),
                     "archived_at IS NULL")
        audit.record(request, "stage.reorder", "stage", stage_id,
                     "Moved stage %s" % request.get("dir", "up"))
        return back_to(request, "/admin/content")

    @app.route("/admin/stages/<int:stage_id>/archive", methods=["POST"])
    @writes
    def stage_archive(request, stage_id):
        stage = content.stage(stage_id)
        if stage is None:
            raise HttpError(404, "Unknown stage.")
        restore = request.checked("restore")
        dependents = db.query("SELECT id, title FROM stages "
                              "WHERE unlock_after_stage_id = ? AND archived_at IS NULL",
                              (stage_id,))
        if not restore and dependents:
            # Re-point the chain so nothing is orphaned behind an archived stage.
            for row in dependents:
                db.execute("UPDATE stages SET unlock_after_stage_id = ?, updated_at = ? "
                           "WHERE id = ?",
                           (stage.unlock_after_stage_id, db.now(), row["id"]))
        db.execute("UPDATE stages SET archived_at = ?, updated_at = ? WHERE id = ?",
                   (None if restore else db.now(), db.now(), stage_id))
        audit.record(request, "stage.restore" if restore else "stage.archive",
                     "stage", stage_id,
                     "%s stage “%s”" % ("Restored" if restore else "Archived",
                                        stage.title))
        request.flash("Stage %s." % ("restored" if restore else "archived"), "ok")
        return back_to(request, "/admin/content")

    # ------------------------------------------------------------ agenda -- #
    @app.route("/admin/stages/<int:stage_id>/agenda/new", methods=["POST"])
    @writes
    def agenda_new(request, stage_id):
        title = request.get("title", "").strip()
        _require({"title": title}, "title", "An agenda item needs a title.")
        item_id = db.insert("agenda_items", {
            "stage_id": stage_id, "title": title,
            "description": request.get("description", "").strip(),
            "sort_order": content.next_sort_order("agenda_items", "stage_id = ?",
                                                  (stage_id,)),
        })
        audit.record(request, "agenda.create", "agenda_item", item_id,
                     "Added agenda item “%s”" % title)
        request.flash("Agenda item added.", "ok")
        return back_to(request, "/admin/stages/%d" % stage_id)

    @app.route("/admin/agenda/<int:item_id>/edit", methods=["POST"])
    @writes
    def agenda_edit(request, item_id):
        row = db.one("SELECT * FROM agenda_items WHERE id = ?", (item_id,))
        if row is None:
            raise HttpError(404, "Unknown agenda item.")
        values = {"title": request.get("title", "").strip(),
                  "description": request.get("description", "").strip()}
        _require(values, "title", "An agenda item needs a title.")
        db.update("agenda_items", item_id, values)
        audit.record(request, "agenda.update", "agenda_item", item_id,
                     "Edited agenda item", before=dict(row), after=values)
        request.flash("Agenda item saved.", "ok")
        return back_to(request, "/admin/stages/%d" % row["stage_id"])

    @app.route("/admin/agenda/<int:item_id>/move", methods=["POST"])
    @writes
    def agenda_move(request, item_id):
        row = db.one("SELECT * FROM agenda_items WHERE id = ?", (item_id,))
        if row is None:
            raise HttpError(404, "Unknown agenda item.")
        content.move("agenda_items", item_id, request.get("dir", "up"),
                     "stage_id = ? AND archived_at IS NULL", (row["stage_id"],))
        return back_to(request, "/admin/stages/%d" % row["stage_id"])

    @app.route("/admin/agenda/<int:item_id>/archive", methods=["POST"])
    @writes
    def agenda_archive(request, item_id):
        row = db.one("SELECT * FROM agenda_items WHERE id = ?", (item_id,))
        if row is None:
            raise HttpError(404, "Unknown agenda item.")
        restore = request.checked("restore")
        db.execute("UPDATE agenda_items SET archived_at = ? WHERE id = ?",
                   (None if restore else db.now(), item_id))
        audit.record(request, "agenda.archive", "agenda_item", item_id,
                     "%s agenda item “%s”"
                     % ("Restored" if restore else "Removed", row["title"]))
        return back_to(request, "/admin/stages/%d" % row["stage_id"])

    # --------------------------------------------------------- components -- #
    @app.route("/admin/stages/<int:stage_id>/components/new", methods=["POST"])
    @writes
    def component_new(request, stage_id):
        values = collect(request, COMPONENT_SPEC)
        _require(values, "title", "A component needs a title.")
        if values["completion_rule"] not in ("sub_items", "self_marked",
                                             "admin_marked"):
            values["completion_rule"] = "sub_items"
        values["stage_id"] = stage_id
        values["key"] = util.unique_key("components",
                                        util.slugify(values["title"], "component"))
        values["sort_order"] = content.next_sort_order(
            "components", "stage_id = ? AND archived_at IS NULL", (stage_id,))
        values["created_at"] = values["updated_at"] = db.now()
        comp_id = db.insert("components", values)
        audit.record(request, "component.create", "component", comp_id,
                     "Created component “%s”" % values["title"], after=values)
        request.flash("Component added.", "ok")
        return redirect("/admin/components/%d" % comp_id)

    @app.route("/admin/components/<int:component_id>")
    @admin_required
    def component_editor(request, component_id):
        comp = content.component(component_id)
        if comp is None:
            raise HttpError(404, "Unknown component.")
        stage = content.stage(comp.stage_id)
        tree = content.sub_item_tree(component_id, include_archived=True,
                                     all_regions=True)
        for node in tree:
            node.region_label = content.region_name(node.region_id)
            node.grade_cohort_label = content.grade_cohort_name(node.grade_cohort_id)
            node.docs = content.documents_for("sub_item", node.id)
            node.item_links = wrap_all(db.query(
                "SELECT * FROM links WHERE sub_item_id = ? ORDER BY sort_order",
                (node.id,)))
            if node.kind == "policy":
                node.quiz = content.quiz_questions(node.id, include_archived=True)
            for child in node.children:
                child.region_label = content.region_name(child.region_id)
                child.grade_cohort_label = content.grade_cohort_name(
                    child.grade_cohort_id)
                child.docs = content.documents_for("sub_item", child.id)
                child.item_links = wrap_all(db.query(
                    "SELECT * FROM links WHERE sub_item_id = ? ORDER BY sort_order",
                    (child.id,)))
                if child.kind == "policy":
                    child.quiz = content.quiz_questions(child.id, include_archived=True)
        groups = [n for n in tree if n.kind == "group" or n.children]
        return render(request, "admin/component_editor.html", comp=comp, stage=stage,
                      tree=tree, groups=groups,
                      comp_links=wrap_all(db.query(
                          "SELECT * FROM links WHERE component_id = ? "
                          "ORDER BY sort_order", (component_id,))),
                      documents=content.documents_for("component", component_id),
                      all_documents=content.all_documents(),
                      history=audit.changes_for("component", component_id, 15))

    @app.route("/admin/components/<int:component_id>/edit", methods=["POST"])
    @writes
    def component_edit(request, component_id):
        comp = content.component(component_id)
        if comp is None:
            raise HttpError(404, "Unknown component.")
        values = collect(request, COMPONENT_SPEC)
        _require(values, "title", "A component needs a title.")
        if values["completion_rule"] not in ("sub_items", "self_marked",
                                             "admin_marked"):
            values["completion_rule"] = comp.completion_rule
        values["updated_at"] = db.now()
        db.update("components", component_id, values)
        audit.record(request, "component.update", "component", component_id,
                     "Edited component “%s”" % values["title"],
                     before=dict(comp), after=values)
        request.flash("Component saved.", "ok")
        return redirect("/admin/components/%d" % component_id)

    @app.route("/admin/components/<int:component_id>/move", methods=["POST"])
    @writes
    def component_move(request, component_id):
        comp = content.component(component_id)
        if comp is None:
            raise HttpError(404, "Unknown component.")
        content.move("components", component_id, request.get("dir", "up"),
                     "stage_id = ? AND archived_at IS NULL", (comp.stage_id,))
        return back_to(request, "/admin/stages/%d" % comp.stage_id)

    @app.route("/admin/components/<int:component_id>/archive", methods=["POST"])
    @writes
    def component_archive(request, component_id):
        comp = content.component(component_id)
        if comp is None:
            raise HttpError(404, "Unknown component.")
        restore = request.checked("restore")
        db.execute("UPDATE components SET archived_at = ?, updated_at = ? WHERE id = ?",
                   (None if restore else db.now(), db.now(), component_id))
        audit.record(request, "component.archive", "component", component_id,
                     "%s component “%s”" % ("Restored" if restore else "Archived",
                                            comp.title))
        request.flash("Component %s." % ("restored" if restore else "archived"), "ok")
        return back_to(request, "/admin/stages/%d" % comp.stage_id)

    # ---------------------------------------------------------- sub-items -- #
    @app.route("/admin/components/<int:component_id>/items/new", methods=["POST"])
    @writes
    def sub_item_new(request, component_id):
        comp = content.component(component_id)
        if comp is None:
            raise HttpError(404, "Unknown component.")
        values = collect(request, SUB_ITEM_SPEC)
        _require(values, "title", "A step needs a title.")
        if values["kind"] not in ("task", "policy", "upload", "link", "group"):
            values["kind"] = "task"
        parent_id = values["parent_id"]
        if parent_id:
            parent = content.sub_item(parent_id)
            if parent is None or parent.component_id != component_id \
                    or parent.parent_id:
                raise HttpError(400, "Invalid parent step.")
        values["component_id"] = component_id
        values["key"] = util.unique_key("sub_items",
                                        util.slugify(values["title"], "step"))
        values["sort_order"] = content.next_sort_order(
            "sub_items",
            "component_id = ? AND COALESCE(parent_id, 0) = ? AND archived_at IS NULL",
            (component_id, parent_id or 0))
        values["created_at"] = values["updated_at"] = db.now()
        item_id = db.insert("sub_items", values)
        audit.record(request, "sub_item.create", "sub_item", item_id,
                     "Created step “%s”" % values["title"], after=values)
        request.flash("Step added.", "ok")
        return back_to(request, "/admin/components/%d" % component_id)

    @app.route("/admin/items/<int:item_id>/edit", methods=["POST"])
    @writes
    def sub_item_edit(request, item_id):
        item = content.sub_item(item_id)
        if item is None:
            raise HttpError(404, "Unknown step.")
        values = collect(request, SUB_ITEM_SPEC)
        _require(values, "title", "A step needs a title.")
        if values["kind"] not in ("task", "policy", "upload", "link", "group"):
            values["kind"] = item.kind
        if values["parent_id"] == item_id:
            values["parent_id"] = item.parent_id
        values["updated_at"] = db.now()
        db.update("sub_items", item_id, values)
        audit.record(request, "sub_item.update", "sub_item", item_id,
                     "Edited step “%s”" % values["title"],
                     before=dict(item), after=values)
        request.flash("Step saved.", "ok")
        return back_to(request, "/admin/components/%d" % item.component_id)

    @app.route("/admin/items/<int:item_id>/move", methods=["POST"])
    @writes
    def sub_item_move(request, item_id):
        item = content.sub_item(item_id)
        if item is None:
            raise HttpError(404, "Unknown step.")
        content.move("sub_items", item_id, request.get("dir", "up"),
                     "component_id = ? AND COALESCE(parent_id, 0) = ? "
                     "AND archived_at IS NULL",
                     (item.component_id, item.parent_id or 0))
        return back_to(request, "/admin/components/%d" % item.component_id)

    @app.route("/admin/items/<int:item_id>/archive", methods=["POST"])
    @writes
    def sub_item_archive(request, item_id):
        item = content.sub_item(item_id)
        if item is None:
            raise HttpError(404, "Unknown step.")
        restore = request.checked("restore")
        stamp = None if restore else db.now()
        db.execute("UPDATE sub_items SET archived_at = ?, updated_at = ? WHERE id = ?",
                   (stamp, db.now(), item_id))
        # Children follow their parent.
        db.execute("UPDATE sub_items SET archived_at = ?, updated_at = ? "
                   "WHERE parent_id = ?", (stamp, db.now(), item_id))
        audit.record(request, "sub_item.archive", "sub_item", item_id,
                     "%s step “%s”" % ("Restored" if restore else "Archived",
                                       item.title))
        request.flash("Step %s." % ("restored" if restore else "archived"), "ok")
        return back_to(request, "/admin/components/%d" % item.component_id)

    # -------------------------------------------------------- CFU quiz -- #
    @app.route("/admin/items/<int:item_id>/quiz/new", methods=["POST"])
    @writes
    def quiz_question_new(request, item_id):
        item = content.sub_item(item_id)
        if item is None:
            raise HttpError(404, "Unknown step.")
        question = request.get("question", "").strip()
        _require({"question": question}, "question", "Enter the question text.")
        correct_index = request.get_int("correct", 1)
        raw_choices = [request.get("choice_%d" % i, "").strip()
                       for i in range(1, 5)]
        # Keep each choice's original position so "correct" still points at
        # the right one even when an earlier choice was left blank.
        choices = [(i, c) for i, c in enumerate(raw_choices, start=1) if c]
        if len(choices) < 2:
            request.flash("Add at least two answer choices.", "error")
            return back_to(request, "/admin/components/%d" % item.component_id)
        if not any(i == correct_index for i, c in choices):
            request.flash("Pick the correct choice from the ones you filled in.",
                          "error")
            return back_to(request, "/admin/components/%d" % item.component_id)
        q_id = db.insert("quiz_questions", {
            "sub_item_id": item_id, "question": question,
            "sort_order": content.next_sort_order(
                "quiz_questions", "sub_item_id = ?", (item_id,)),
            "created_at": db.now(), "archived_at": None,
        })
        for sort, (orig_index, choice) in enumerate(choices, start=1):
            db.insert("quiz_choices", {
                "question_id": q_id, "label": choice,
                "is_correct": 1 if orig_index == correct_index else 0,
                "sort_order": sort * 10,
            })
        audit.record(request, "quiz_question.create", "quiz_question", q_id,
                     "Added a quiz question to “%s”" % item.title)
        request.flash("Question added.", "ok")
        return back_to(request, "/admin/components/%d" % item.component_id)

    @app.route("/admin/quiz-questions/<int:question_id>/archive", methods=["POST"])
    @writes
    def quiz_question_archive(request, question_id):
        row = db.one("SELECT * FROM quiz_questions WHERE id = ?", (question_id,))
        if row is None:
            raise HttpError(404, "Unknown question.")
        item = content.sub_item(row["sub_item_id"])
        db.execute("UPDATE quiz_questions SET archived_at = ? WHERE id = ?",
                   (db.now(), question_id))
        audit.record(request, "quiz_question.archive", "quiz_question", question_id,
                     "Removed a quiz question")
        request.flash("Question removed.", "ok")
        return back_to(request, "/admin/components/%d" % item.component_id
                       if item else "/admin/content")

    # ==================================================================== #
    # Links
    # ==================================================================== #

    @app.route("/admin/links")
    @admin_required
    def links_page(request):
        rows = content.all_links()
        for row in rows:
            row.region_label = content.region_name(row.region_id)
            row.grade_cohort_label = content.grade_cohort_name(row.grade_cohort_id)
            row.target = "Global"
            if row.stage_id:
                stage = content.stage(row.stage_id)
                row.target = "Stage · %s" % (stage.title if stage else row.stage_id)
            elif row.component_id:
                comp = content.component(row.component_id)
                row.target = "Component · %s" % (comp.title if comp else row.component_id)
            elif row.sub_item_id:
                item = content.sub_item(row.sub_item_id)
                row.target = "Step · %s" % (item.title if item else row.sub_item_id)
        return render(request, "admin/links.html", links=rows,
                      stages=content.stages(),
                      components=wrap_all(db.query(
                          "SELECT * FROM components WHERE archived_at IS NULL "
                          "ORDER BY stage_id, sort_order")),
                      items=wrap_all(db.query(
                          "SELECT * FROM sub_items WHERE archived_at IS NULL "
                          "AND kind != 'group' ORDER BY component_id, sort_order")))

    @app.route("/admin/links/new", methods=["POST"])
    @writes
    def link_new(request):
        values = collect(request, LINK_SPEC)
        _require(values, "label", "A link needs a label.")
        _require(values, "url", "A link needs a URL.")
        target_type = request.get("target_type", "global")
        target_id = request.get_int("target_id")
        values.update({"stage_id": None, "component_id": None, "sub_item_id": None})
        if target_type in ("stage", "component", "sub_item"):
            if not target_id:
                raise HttpError(400, "Choose what this link is attached to.")
            values[{"stage": "stage_id", "component": "component_id",
                    "sub_item": "sub_item_id"}[target_type]] = target_id
        values["key"] = util.unique_key("links", util.slugify(values["label"], "link"))
        values["sort_order"] = content.next_sort_order("links")
        values["created_at"] = values["updated_at"] = db.now()
        link_id = db.insert("links", values)
        audit.record(request, "link.create", "link", link_id,
                     "Created link “%s”" % values["label"], after=values)
        request.flash("Link added.", "ok")
        return back_to(request, "/admin/links")

    @app.route("/admin/links/<int:link_id>/edit", methods=["POST"])
    @writes
    def link_edit(request, link_id):
        row = db.one("SELECT * FROM links WHERE id = ?", (link_id,))
        if row is None:
            raise HttpError(404, "Unknown link.")
        values = collect(request, LINK_SPEC)
        _require(values, "label", "A link needs a label.")
        _require(values, "url", "A link needs a URL.")
        values["updated_at"] = db.now()
        db.update("links", link_id, values)
        audit.record(request, "link.update", "link", link_id,
                     "Edited link “%s”" % values["label"],
                     before=dict(row), after=values)
        request.flash("Link saved.", "ok")
        return back_to(request, "/admin/links")

    @app.route("/admin/links/<int:link_id>/delete", methods=["POST"])
    @writes
    def link_delete(request, link_id):
        row = db.one("SELECT * FROM links WHERE id = ?", (link_id,))
        if row is None:
            raise HttpError(404, "Unknown link.")
        db.execute("DELETE FROM links WHERE id = ?", (link_id,))
        audit.record(request, "link.delete", "link", link_id,
                     "Deleted link “%s”" % row["label"], before=dict(row))
        request.flash("Link removed.", "ok")
        return back_to(request, "/admin/links")

    # ==================================================================== #
    # Documents & versions
    # ==================================================================== #

    def _doc_profiles(kind):
        if kind == "video":
            # An image is allowed as a stand-in until the real video ships.
            return ["video", "image"]
        if kind in ("sample", "deck", "guide"):
            return ["document", "image", "video"]
        return ["document"]

    @app.route("/admin/documents")
    @admin_required
    def documents_page(request):
        docs = content.all_documents(include_archived=request.checked("archived"))
        for doc in docs:
            doc.region_label = content.region_name(doc.region_id)
            doc.grade_cohort_label = content.grade_cohort_name(doc.grade_cohort_id)
            doc.target = "Unattached"
            if doc.stage_id:
                stage = content.stage(doc.stage_id)
                doc.target = "Stage · %s" % (stage.title if stage else doc.stage_id)
            elif doc.component_id:
                comp = content.component(doc.component_id)
                doc.target = "Component · %s" % (comp.title if comp else "?")
            elif doc.sub_item_id:
                item = content.sub_item(doc.sub_item_id)
                doc.target = "Step · %s" % (item.title if item else "?")
        return render(request, "admin/documents.html", documents=docs,
                      show_archived=request.checked("archived"),
                      stages=content.stages(),
                      components=wrap_all(db.query(
                          "SELECT * FROM components WHERE archived_at IS NULL "
                          "ORDER BY stage_id, sort_order")),
                      items=wrap_all(db.query(
                          "SELECT * FROM sub_items WHERE archived_at IS NULL "
                          "AND kind != 'group' ORDER BY component_id, sort_order")))

    @app.route("/admin/documents/new", methods=["POST"])
    @writes
    def document_new(request):
        values = collect(request, DOC_SPEC)
        _require(values, "title", "A document needs a title.")
        if values["kind"] not in ("policy", "deck", "video", "sample", "guide"):
            values["kind"] = "policy"
        target_type = request.get("target_type", "none")
        target_id = request.get_int("target_id")
        values.update({"stage_id": None, "component_id": None, "sub_item_id": None})
        if target_type in ("stage", "component", "sub_item"):
            if not target_id:
                raise HttpError(400, "Choose what this document belongs to.")
            values[{"stage": "stage_id", "component": "component_id",
                    "sub_item": "sub_item_id"}[target_type]] = target_id
        values["key"] = util.unique_key("documents",
                                        util.slugify(values["title"], "document"))
        values["created_at"] = values["updated_at"] = db.now()
        doc_id = db.insert("documents", values)
        audit.record(request, "document.create", "document", doc_id,
                     "Created document “%s”" % values["title"], after=values)

        upload = request.file("file")
        if upload:
            try:
                _store_version(request, doc_id, upload, values["kind"],
                               request.get("effective_from", ""),
                               request.get("notes", ""))
            except storage.ValidationError as exc:
                request.flash("Document created, but the file was rejected: %s" % exc,
                              "error")
                return back_to(request, "/admin/documents/%d" % doc_id)
        request.flash("Document added.", "ok")
        return back_to(request, "/admin/documents/%d" % doc_id)

    def _store_version(request, doc_id, upload, kind, effective_from, notes):
        mime = storage.validate_any(upload, _doc_profiles(kind))
        key = storage.save("documents/%d" % doc_id,
                           storage.safe_filename(upload.filename), upload.data)
        version_no = content.next_version_no(doc_id)
        version_id = db.insert("document_versions", {
            "document_id": doc_id, "version_no": version_no,
            "filename": storage.safe_filename(upload.filename),
            "storage_key": key, "mime_type": mime, "size_bytes": upload.size,
            "effective_from": effective_from.strip() or db.now(),
            "notes": notes.strip(),
            "uploaded_by": request.user["id"], "created_at": db.now(),
        })
        db.execute("UPDATE documents SET updated_at = ? WHERE id = ?",
                   (db.now(), doc_id))
        audit.record(request, "document.version", "document", doc_id,
                     "Uploaded version %d (%s)" % (version_no, upload.filename))
        return version_id

    @app.route("/admin/documents/<int:doc_id>")
    @admin_required
    def document_detail(request, doc_id):
        doc = content.document(doc_id)
        if doc is None:
            raise HttpError(404, "Unknown document.")
        doc.current = content.current_version(doc_id)
        ack_count = db.scalar(
            "SELECT COUNT(*) FROM policy_acknowledgements a "
            "JOIN document_versions v ON v.id = a.document_version_id "
            "WHERE v.document_id = ?", (doc_id,), 0)
        return render(request, "admin/document_detail.html", doc=doc,
                      versions=content.versions(doc_id), ack_count=ack_count,
                      history=audit.changes_for("document", doc_id, 20),
                      target_label=content.region_name(doc.region_id))

    @app.route("/admin/documents/<int:doc_id>/edit", methods=["POST"])
    @writes
    def document_edit(request, doc_id):
        doc = content.document(doc_id)
        if doc is None:
            raise HttpError(404, "Unknown document.")
        values = collect(request, DOC_SPEC)
        _require(values, "title", "A document needs a title.")
        values["updated_at"] = db.now()
        db.update("documents", doc_id, values)
        audit.record(request, "document.update", "document", doc_id,
                     "Edited document “%s”" % values["title"],
                     before=dict(doc), after=values)
        request.flash("Document saved.", "ok")
        return redirect("/admin/documents/%d" % doc_id)

    @app.route("/admin/documents/<int:doc_id>/version", methods=["POST"])
    @writes
    def document_version(request, doc_id):
        doc = content.document(doc_id)
        if doc is None:
            raise HttpError(404, "Unknown document.")
        upload = request.file("file")
        try:
            _store_version(request, doc_id, upload, doc.kind,
                           request.get("effective_from", ""),
                           request.get("notes", ""))
        except storage.ValidationError as exc:
            request.flash(str(exc), "error")
            return redirect("/admin/documents/%d" % doc_id)
        request.flash("New version uploaded. Tutors will see it from its "
                      "effective date; earlier versions stay on record.", "ok")
        return redirect("/admin/documents/%d" % doc_id)

    @app.route("/admin/documents/<int:doc_id>/archive", methods=["POST"])
    @writes
    def document_archive(request, doc_id):
        doc = content.document(doc_id)
        if doc is None:
            raise HttpError(404, "Unknown document.")
        restore = request.checked("restore")
        db.execute("UPDATE documents SET archived_at = ?, updated_at = ? WHERE id = ?",
                   (None if restore else db.now(), db.now(), doc_id))
        audit.record(request, "document.archive", "document", doc_id,
                     "%s document “%s”" % ("Restored" if restore else "Archived",
                                           doc.title))
        return back_to(request, "/admin/documents")

    @app.route("/admin/versions/<int:version_id>/download")
    @admin_required
    def version_download(request, version_id):
        row = db.one("SELECT * FROM document_versions WHERE id = ?", (version_id,))
        if row is None or not storage.exists(row["storage_key"]):
            raise HttpError(404, "That file is missing.")
        return file_response(storage.local_path(row["storage_key"]),
                             download_name=row["filename"])

    # ==================================================================== #
    # Regions
    # ==================================================================== #

    @app.route("/admin/regions")
    @admin_required
    def regions_page(request):
        rows = content.regions(active_only=False)
        for row in rows:
            row.tutor_count = db.scalar(
                "SELECT COUNT(*) FROM users WHERE region_id = ?", (row.id,), 0)
            row.content_count = db.scalar(
                "SELECT (SELECT COUNT(*) FROM components WHERE region_id = ?) + "
                "(SELECT COUNT(*) FROM sub_items WHERE region_id = ?) + "
                "(SELECT COUNT(*) FROM documents WHERE region_id = ?)",
                (row.id, row.id, row.id), 0)
        return render(request, "admin/regions.html", regions=rows)

    @app.route("/admin/regions/new", methods=["POST"])
    @writes
    def region_new(request):
        name = request.get("name", "").strip()
        if not name:
            raise HttpError(400, "A region needs a name.")
        region_id = db.insert("regions", {
            "key": util.unique_key("regions", util.slugify(name, "region")),
            "name": name,
            "sort_order": content.next_sort_order("regions"),
            "is_active": 1 if request.checked("is_active") else 0,
        })
        audit.record(request, "region.create", "region", region_id,
                     "Created region “%s”" % name)
        request.flash("Region added.", "ok")
        return redirect("/admin/regions")

    @app.route("/admin/regions/<int:region_id>/edit", methods=["POST"])
    @writes
    def region_edit(request, region_id):
        row = db.one("SELECT * FROM regions WHERE id = ?", (region_id,))
        if row is None:
            raise HttpError(404, "Unknown region.")
        values = {"name": request.get("name", "").strip() or row["name"],
                  "is_active": 1 if request.checked("is_active") else 0}
        db.update("regions", region_id, values)
        audit.record(request, "region.update", "region", region_id,
                     "Edited region “%s”" % values["name"],
                     before=dict(row), after=values)
        request.flash("Region saved.", "ok")
        return redirect("/admin/regions")

    # ==================================================================== #
    # Orientation sessions
    # ==================================================================== #

    @app.route("/admin/orientation")
    @admin_required
    def orientation_page(request):
        sessions = wrap_all(db.query(
            "SELECT * FROM orientation_sessions ORDER BY (starts_at IS NULL), "
            "starts_at DESC, id DESC"))
        for session in sessions:
            session.region_label = content.region_name(session.region_id)
            session.invited = db.scalar(
                "SELECT COUNT(*) FROM orientation_invites WHERE session_id = ?",
                (session.id,), 0)
            session.attended = db.scalar(
                "SELECT COUNT(*) FROM orientation_attendance WHERE session_id = ? "
                "AND attended = 1", (session.id,), 0)
            session.deck = (content.document(session.deck_document_id)
                            if session.deck_document_id else None)
        stage = progress.orientation_stage()
        return render(request, "admin/orientation.html", sessions=sessions,
                      stage=stage,
                      agenda=content.agenda_items(stage.id) if stage else [],
                      decks=[d for d in content.all_documents()
                             if d.kind in ("deck", "guide")])

    @app.route("/admin/orientation/new", methods=["POST"])
    @writes
    def orientation_new(request):
        values = collect(request, SESSION_SPEC)
        _require(values, "title", "The session needs a title.")
        values["created_at"] = db.now()
        session_id = db.insert("orientation_sessions", values)
        audit.record(request, "orientation.create", "orientation_session", session_id,
                     "Created session “%s”" % values["title"], after=values)
        request.flash("Session added.", "ok")
        return redirect("/admin/orientation")

    @app.route("/admin/orientation/<int:session_id>/edit", methods=["POST"])
    @writes
    def orientation_edit(request, session_id):
        row = db.one("SELECT * FROM orientation_sessions WHERE id = ?", (session_id,))
        if row is None:
            raise HttpError(404, "Unknown session.")
        values = collect(request, SESSION_SPEC)
        _require(values, "title", "The session needs a title.")
        db.update("orientation_sessions", session_id, values)
        audit.record(request, "orientation.update", "orientation_session", session_id,
                     "Edited session “%s”" % values["title"],
                     before=dict(row), after=values)
        request.flash("Session saved.", "ok")
        return redirect("/admin/orientation")

    @app.route("/admin/orientation/<int:session_id>/invite", methods=["POST"])
    @writes
    def orientation_invite(request, session_id):
        session = db.one("SELECT * FROM orientation_sessions WHERE id = ?",
                         (session_id,))
        if session is None:
            raise HttpError(404, "Unknown session.")
        sql = ["SELECT * FROM users WHERE role_key = 'tutor' AND is_active = 1"]
        args = []
        if session["region_id"]:
            sql.append("AND (region_id IS NULL OR region_id = ?)")
            args.append(session["region_id"])
        if not request.checked("include_attended"):
            sql.append("AND id NOT IN (SELECT user_id FROM orientation_attendance "
                       "WHERE attended = 1)")
        tutors = wrap_all(db.query(" ".join(sql), args))
        for tutor in tutors:
            progress.invite_to_orientation(tutor, session_id)
        audit.record(request, "orientation.invite", "orientation_session", session_id,
                     "Invited %d tutor(s)" % len(tutors))
        request.flash("Invited %s." % util.plural(len(tutors), "tutor"), "ok")
        return redirect("/admin/orientation")

    # ==================================================================== #
    # Class-with-a-student slots
    # ==================================================================== #

    @app.route("/admin/class-slots")
    @admin_required
    def class_slots_page(request):
        slots = wrap_all(db.query(
            "SELECT cs.*, u.name AS tutor_name FROM class_slots cs "
            "LEFT JOIN users u ON u.id = cs.tutor_id "
            "ORDER BY (cs.status = 'cancelled'), cs.starts_at"))
        for slot in slots:
            slot.region_label = content.region_name(slot.region_id)
            slot.grade_cohort_label = (
                content.grade_cohort_name(slot.grade_cohort_id)
                if slot.grade_cohort_id else "Any grade")
        return render(request, "admin/class_slots.html", slots=slots)

    @app.route("/admin/class-slots/new", methods=["POST"])
    @writes
    def class_slot_new(request):
        values = collect(request, CLASS_SLOT_SPEC)
        _require(values, "starts_at", "A slot needs a date and time.")
        values.update(status="open", tutor_id=None, booked_at=None,
                      created_at=db.now(), updated_at=db.now())
        slot_id = db.insert("class_slots", values)
        audit.record(request, "class_slot.create", "class_slot", slot_id,
                     "Added a class slot for %s"
                     % (values["student_name"] or "a student"), after=values)
        request.flash("Slot added — tutors can now pick it.", "ok")
        return redirect("/admin/class-slots")

    @app.route("/admin/class-slots/<int:slot_id>/release", methods=["POST"])
    @writes
    def class_slot_release(request, slot_id):
        slot = db.one("SELECT * FROM class_slots WHERE id = ?", (slot_id,))
        if slot is None:
            raise HttpError(404, "Unknown slot.")
        db.execute(
            "UPDATE class_slots SET tutor_id = NULL, status = 'open', "
            "booked_at = NULL, updated_at = ? WHERE id = ?", (db.now(), slot_id))
        audit.record(request, "class_slot.release", "class_slot", slot_id,
                     "Released slot back to open")
        request.flash("Slot reopened for booking.", "ok")
        return back_to(request, "/admin/class-slots")

    @app.route("/admin/class-slots/<int:slot_id>/delete", methods=["POST"])
    @writes
    def class_slot_delete(request, slot_id):
        slot = db.one("SELECT * FROM class_slots WHERE id = ?", (slot_id,))
        if slot is None:
            raise HttpError(404, "Unknown slot.")
        if slot["status"] != "open":
            request.flash("Release the slot before deleting it.", "error")
            return back_to(request, "/admin/class-slots")
        db.execute("DELETE FROM class_slots WHERE id = ?", (slot_id,))
        audit.record(request, "class_slot.delete", "class_slot", slot_id,
                     "Deleted an open slot")
        request.flash("Slot removed.", "ok")
        return back_to(request, "/admin/class-slots")

    # ------------------------------------------------- bulk class-slot CSV -- #
    @app.route("/admin/class-slots/import", methods=["GET", "POST"])
    @admin_required
    def class_slots_import(request):
        if request.method == "GET":
            return render(request, "admin/class_slots_import.html", result=None)

        request.verify_csrf()
        from .auth import can_write
        if not can_write(request):
            raise HttpError(403, "Your account has read-only access.")
        upload = request.file("file")
        if not upload:
            request.flash("Choose a CSV file to upload.", "error")
            return redirect("/admin/class-slots/import")
        try:
            text = upload.data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = upload.data.decode("latin-1")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any((c or "").strip() for c in r)]
        if not rows:
            request.flash("That file looks empty.", "error")
            return redirect("/admin/class-slots/import")

        header = [c.strip().lower() for c in rows[0]]
        known = {"student", "student name", "name", "date", "class date",
                 "time", "class time", "grade", "grade/subject", "grade_subject",
                 "grade group", "cohort", "grade cohort"}
        has_header = bool(known & set(header))
        data_rows = rows[1:] if has_header else rows

        def column(names):
            for name in names:
                if name in header:
                    return header.index(name)
            return None

        idx_name = column(["student name", "student", "name"]) if has_header else 0
        idx_date = column(["class date", "date"]) if has_header else 1
        idx_time = column(["class time", "time"]) if has_header else 2
        idx_grade = (column(["grade", "grade/subject", "grade & subject",
                            "grade_subject"]) if has_header else 3)
        idx_duration = column(["duration", "duration (min)", "duration_minutes"])
        idx_region = column(["region"])
        idx_notes = column(["notes", "note"])
        idx_cohort = column(["grade group", "grade cohort", "cohort"])

        region_by_name = {r.name.strip().lower(): r.id
                         for r in content.regions(active_only=False)}
        cohort_by_name = {g.name.strip().lower(): g.id
                          for g in content.grade_cohorts(active_only=False)}

        added, skipped = [], []
        for row in data_rows:
            def cell(index):
                if index is None or index >= len(row):
                    return ""
                return (row[index] or "").strip()

            student_name = cell(idx_name)
            date_text = cell(idx_date)
            time_text = cell(idx_time)
            grade_text = cell(idx_grade)
            label = student_name or " ".join(row).strip()[:40] or "row"

            starts_at = _parse_class_datetime(date_text, time_text)
            if starts_at is None:
                skipped.append("%s — couldn't read the date/time (%r / %r)"
                              % (label, date_text, time_text))
                continue

            duration = None
            if idx_duration is not None:
                try:
                    duration = int(cell(idx_duration))
                except ValueError:
                    duration = None
            duration = duration or 60

            region_id = (region_by_name.get(cell(idx_region).lower())
                        if idx_region is not None else None)
            cohort_text = cell(idx_cohort) if idx_cohort is not None else ""
            cohort_id = cohort_by_name.get(cohort_text.lower()) if cohort_text else None
            if cohort_text and cohort_id is None:
                skipped.append("%s — unknown grade group \u201c%s\u201d" % (label, cohort_text))
                continue

            values = {
                "starts_at": starts_at, "duration_minutes": duration,
                "student_name": student_name, "grade_subject": grade_text,
                "region_id": region_id, "grade_cohort_id": cohort_id,
                "notes": cell(idx_notes),
                "status": "open", "tutor_id": None, "booked_at": None,
                "created_at": db.now(), "updated_at": db.now(),
            }
            db.insert("class_slots", values)
            added.append("%s — %s" % (label, starts_at))

        audit.record(request, "class_slot.import", "class_slot", None,
                     "CSV import: %d added, %d skipped" % (len(added), len(skipped)))
        request.flash(
            "%s added.%s" % (
                util.plural(len(added), "slot"),
                " %d skipped — see details below." % len(skipped) if skipped else ""),
            "ok" if added else "error")
        return render(request, "admin/class_slots_import.html",
                      result={"added": added[:50], "skipped": skipped[:50],
                              "added_count": len(added),
                              "skipped_count": len(skipped)})

    # ==================================================================== #
    # Tutor management
    # ==================================================================== #

    @app.route("/admin/tutors")
    @admin_required
    def tutors_page(request):
        region_id = request.get_int("region_id")
        grade_cohort_id = request.get_int("grade_cohort_id")
        stage_id = request.get_int("stage_id")
        status = request.get("status", "")
        search = request.get("q", "").strip()
        stalled_days = request.get_int("stalled")
        captain_id = captain_scope(request)
        rows = progress.tutors(region_id=region_id, grade_cohort_id=grade_cohort_id,
                               stage_id=stage_id, status=status or None,
                               search=search, stalled_days=stalled_days,
                               captain_id=captain_id)
        for row in rows:
            row.region_label = content.region_name(row.region_id)
            row.grade_cohort_label = content.grade_cohort_name(row.grade_cohort_id)
            row.captain_label = content.captain_name(row.captain_id)
        is_captain_view = request.role and request.role["key"] == "viewer"
        return render(request, "admin/tutors.html", tutors=rows,
                      stages=content.stages(), region_id=region_id,
                      grade_cohort_id=grade_cohort_id,
                      stage_id=stage_id, status=status, q=search,
                      stalled=stalled_days, captain_id=captain_id,
                      is_captain_view=is_captain_view,
                      captains_all=content.captains(),
                      default_stalled=db.setting_int("stalled_days", 7))

    @app.route("/admin/tutors.csv")
    @admin_required
    def tutors_csv(request):
        rows = progress.tutors(region_id=request.get_int("region_id"),
                               grade_cohort_id=request.get_int("grade_cohort_id"),
                               stage_id=request.get_int("stage_id"),
                               status=request.get("status", "") or None,
                               search=request.get("q", "").strip(),
                               stalled_days=request.get_int("stalled"),
                               captain_id=captain_scope(request))
        stages = content.stages()
        header = ["Name", "Email", "Phone", "Region", "Grade cohort",
                  "Current stage", "Completion %", "Stages complete", "Signed up",
                  "Last activity", "Journey completed"] + \
                 ["%s status" % s.title for s in stages]
        out = []
        for row in rows:
            by_id = {s.id: s for s in row.states}
            out.append([
                row.name, row.email or "", row.phone or "",
                content.region_name(row.region_id),
                content.grade_cohort_name(row.grade_cohort_id),
                row.current_stage_title, row.summary.percent,
                "%d/%d" % (row.summary.stages_done, row.summary.stages_total),
                row.created_at, row.last_activity_at or "", row.completed_at or "",
            ] + [(by_id[s.id].status if s.id in by_id else "") for s in stages])
        audit.record(request, "report.export_csv", "report", None,
                     "Exported %d tutor row(s)" % len(out))
        return Response(util.csv_bytes(header, out),
                        content_type="text/csv; charset=utf-8",
                        headers=[("Content-Disposition",
                                  'attachment; filename="tutor-progress.csv"')])

    # ------------------------------------------------- bulk tutor creation -- #
    @app.route("/admin/tutors/bulk-create", methods=["GET", "POST"])
    @admin_required
    def tutors_bulk_create(request):
        if request.method == "GET":
            return render(request, "admin/tutors_bulk_create.html", result=None)

        request.verify_csrf()
        from .auth import can_write
        if not can_write(request):
            raise HttpError(403, "Your account has read-only access.")
        upload = request.file("file")
        if not upload:
            request.flash("Choose a CSV file to upload.", "error")
            return redirect("/admin/tutors/bulk-create")
        try:
            text = upload.data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = upload.data.decode("latin-1")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any((c or "").strip() for c in r)]
        if not rows:
            request.flash("That file looks empty.", "error")
            return redirect("/admin/tutors/bulk-create")

        header = [c.strip().lower() for c in rows[0]]
        known = {"email", "phone", "mobile", "name", "region", "grade",
                 "grade cohort", "cohort", "db id", "db_id", "dbid"}
        has_header = bool(known & set(header))
        data_rows = rows[1:] if has_header else rows

        def column(names):
            for name in names:
                if name in header:
                    return header.index(name)
            return None

        idx_name = column(["name", "tutor", "tutor name"]) if has_header else 0
        idx_email = column(["email", "email address"]) if has_header else 1
        idx_phone = column(["phone", "mobile", "phone number"]) if has_header else 2
        idx_region = column(["region"]) if has_header else None
        idx_grade = (column(["grade", "grade cohort", "cohort"])
                    if has_header else None)
        idx_db_id = (column(["db id", "db_id", "dbid", "teacher db id"])
                    if has_header else None)

        region_by_name = {r.name.strip().lower(): r.id
                          for r in content.regions(active_only=False)}
        grade_cohort_by_name = {g.name.strip().lower(): g.id
                                for g in content.grade_cohorts(active_only=False)}

        created, skipped = [], []
        for row in data_rows:
            def cell(index):
                if index is None or index >= len(row):
                    return ""
                return (row[index] or "").strip()

            name = cell(idx_name)
            email = security.normalise_email(cell(idx_email))
            phone = security.normalise_phone(cell(idx_phone))
            region_name = cell(idx_region)
            grade_name = cell(idx_grade)
            db_id = cell(idx_db_id)
            label = email or phone or name or " ".join(row)[:40]

            if not name:
                skipped.append("%s — missing name" % label)
            elif not phone:
                skipped.append("%s — needs a phone number to generate a password"
                               % label)
            elif len(re.sub(r"\D", "", phone)) < 8:
                skipped.append("%s — phone number looks invalid" % label)
            elif email and not security.EMAIL_RE.match(email):
                skipped.append("%s — email address looks invalid" % label)
            elif email and db.one("SELECT 1 FROM users WHERE email = ?", (email,)):
                skipped.append("%s — account already exists" % label)
            elif db.one("SELECT 1 FROM users WHERE phone = ?", (phone,)):
                skipped.append("%s — account already exists" % label)
            elif grade_name and grade_name.lower() not in grade_cohort_by_name:
                skipped.append("%s — unknown grade cohort “%s”" % (label, grade_name))
            elif db_id and db.one("SELECT 1 FROM users WHERE db_id = ?", (db_id,)):
                skipped.append("%s — DB ID “%s” is already assigned to another "
                               "account" % (label, db_id))
            else:
                region_id = (region_by_name.get(region_name.lower())
                            if region_name else None)
                grade_cohort_id = (grade_cohort_by_name.get(grade_name.lower())
                                   if grade_name else None)
                password = security.generate_password(phone)
                user_id = db.insert("users", {
                    "name": name, "email": email or None, "phone": phone,
                    "password_hash": security.hash_password(password),
                    "role_key": "tutor", "region_id": region_id,
                    "grade_cohort_id": grade_cohort_id, "db_id": db_id or None,
                    "created_at": db.now(), "last_activity_at": db.now(),
                })
                user = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
                session = progress.orientation_session_for(user)
                if session is not None:
                    progress.invite_to_orientation(user, session["id"])
                progress.sync(user, notifications=False)
                created.append({"name": name, "email": email, "phone": phone,
                                "password": password, "db_id": db_id,
                                "grade_cohort": content.grade_cohort_name(
                                    grade_cohort_id)})

        audit.record(request, "tutor.bulk_create", "report", None,
                     "Bulk-created %d tutor account(s), %d skipped"
                     % (len(created), len(skipped)))
        return render(request, "admin/tutors_bulk_create.html",
                      result={"created": created, "skipped": skipped[:50],
                              "skipped_count": len(skipped)})

    @app.route("/admin/tutors/directors", methods=["GET", "POST"])
    @admin_required
    def tutors_assign_directors(request):
        """Bulk version of the per-tutor Activation Director dropdown: a sheet
        of tutor -> director, matched on whatever identifier the sheet happens
        to carry."""
        if request.method == "GET":
            return render(request, "admin/tutors_directors.html", result=None,
                          captains_all=content.captains())

        request.verify_csrf()
        from .auth import can_write
        if not can_write(request):
            raise HttpError(403, "Your account has read-only access.")
        upload = request.file("file")
        if not upload:
            request.flash("Choose a CSV file to upload.", "error")
            return redirect("/admin/tutors/directors")
        try:
            text = upload.data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = upload.data.decode("latin-1")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any((c or "").strip() for c in r)]
        if not rows:
            request.flash("That file looks empty.", "error")
            return redirect("/admin/tutors/directors")

        header = [c.strip().lower() for c in rows[0]]
        known = {"tutor", "director", "activation director", "email", "phone",
                 "db id", "db_id", "dbid", "tutor email", "director email"}
        has_header = bool(known & set(header))
        data_rows = rows[1:] if has_header else rows

        def column(names):
            for name in names:
                if name in header:
                    return header.index(name)
            return None

        idx_tutor = (column(["tutor", "tutor email", "email", "phone", "db id",
                             "db_id", "dbid"]) if has_header else 0)
        idx_director = (column(["director", "activation director",
                                "director email"]) if has_header else 1)

        assigned, skipped = [], []
        for row in data_rows:
            def cell(index):
                if index is None or index >= len(row):
                    return ""
                return (row[index] or "").strip()

            tutor_ref = cell(idx_tutor)
            director_ref = cell(idx_director)
            if not tutor_ref or not director_ref:
                skipped.append("%s — needs both a tutor and a director"
                               % (" ".join(row)[:40] or "blank row"))
                continue

            tutor = _find_tutor(tutor_ref)
            if tutor is None:
                skipped.append("%s — no tutor matches that email, phone or DB ID"
                               % tutor_ref)
                continue
            director = wrap(db.one(
                "SELECT * FROM users WHERE email = ? AND role_key IN "
                "('admin', 'viewer') AND is_active = 1",
                (security.normalise_email(director_ref),)))
            if director is None:
                skipped.append("%s — “%s” is not an active team member"
                               % (tutor_ref, director_ref))
                continue

            db.update("users", tutor.id, {"captain_id": director.id})
            assigned.append({"tutor": tutor.name,
                             "identifier": tutor.email or tutor.phone,
                             "director": director.name})

        audit.record(request, "tutor.assign_directors", "report", None,
                     "Assigned Activation Directors for %d tutor(s), %d skipped"
                     % (len(assigned), len(skipped)))
        return render(request, "admin/tutors_directors.html",
                      captains_all=content.captains(),
                      result={"assigned": assigned, "skipped": skipped[:50],
                              "skipped_count": len(skipped)})

    def _find_tutor(ref):
        """Match a tutor on email, phone or DB ID — whichever the sheet used."""
        email = security.normalise_email(ref)
        phone = security.normalise_phone(ref)
        for sql, arg in (("email = ?", email), ("phone = ?", phone),
                         ("db_id = ?", ref)):
            if not arg:
                continue
            row = db.one("SELECT * FROM users WHERE %s AND role_key = 'tutor'"
                         % sql, (arg,))
            if row:
                return wrap(row)
        return None

    @app.route("/admin/tutors/<int:user_id>")
    @admin_required
    def tutor_detail(request, user_id):
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ? AND role_key = 'tutor'",
                            (user_id,)))
        if tutor is None:
            raise HttpError(404, "Unknown tutor.")
        if (request.role and request.role["key"] == "viewer"
                and tutor.captain_id != request.user["id"]):
            raise HttpError(403, "That tutor isn't assigned to you.")
        states = progress.sync(tutor, notifications=False)
        for state in states:
            for comp in state.components:
                comp.tree = progress.hydrate_tree(tutor, comp, tutor.region_id,
                                                  tutor.grade_cohort_id)
        return render(request, "admin/tutor_detail.html", tutor=tutor, states=states,
                      summary=progress.overall(states),
                      region_label=content.region_name(tutor.region_id),
                      grade_cohort_label=content.grade_cohort_name(
                          tutor.grade_cohort_id),
                      captain_label=content.captain_name(tutor.captain_id),
                      captains_all=content.captains(),
                      acks=progress.acknowledgements_for(user_id),
                      submissions=progress.submissions_for(user_id,
                                                           include_superseded=True),
                      notifications=notify.for_user(user_id, 15),
                      stalled_days=db.days_since(tutor.last_activity_at
                                                 or tutor.created_at),
                      class_reviews=progress.class_review_state(user_id),
                      next_class=progress.next_pending_class(user_id),
                      compliance=progress.compliance_state(user_id),
                      compliance_event_labels=progress.COMPLIANCE_EVENT_LABELS,
                      can_review=can_write_tutor(request, tutor.captain_id))

    @app.route("/admin/tutors/<int:user_id>/classes", methods=["POST"])
    @captain_write_required
    def tutor_class_review(request, user_id):
        class_number = request.get_int("class_number")
        if not class_number:
            raise HttpError(400, "Missing class number.")
        feedback_note = request.get("feedback_note", "").strip()
        red_flag_reason = (request.get("red_flag_reason", "").strip()
                           if request.checked("red_flag") else "")
        _require({"feedback_note": feedback_note}, "feedback_note",
                 "Add a feedback note before logging this class.")
        try:
            progress.log_class_review(request, user_id, class_number,
                                      feedback_note, red_flag_reason)
        except ValueError as exc:
            raise HttpError(400, str(exc))
        return back_to(request, "/admin/tutors/%d" % user_id)

    @app.route("/admin/tutors/<int:user_id>/compliance", methods=["POST"])
    @captain_write_required
    def tutor_compliance_event(request, user_id):
        event_type = request.get("event_type", "")
        if event_type not in progress.COMPLIANCE_EVENT_LABELS:
            raise HttpError(400, "Unknown compliance event type.")
        notes = request.get("notes", "").strip()
        progress.log_compliance_event(request, user_id, event_type, notes)
        return back_to(request, "/admin/tutors/%d" % user_id)

    @app.route("/admin/tutors/<int:user_id>/attendance", methods=["POST"])
    @writes
    def tutor_attendance(request, user_id):
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        if tutor is None:
            raise HttpError(404, "Unknown tutor.")
        attended = request.checked("attended")
        progress.set_orientation_attendance(request.user, tutor, attended,
                                            request.get_int("session_id"))
        audit.record(request, "tutor.attendance", "user", user_id,
                     "Marked Orientation %s" % ("attended" if attended
                                                else "not attended"))
        request.flash("Attendance updated.", "ok")
        return back_to(request, "/admin/tutors/%d" % user_id)

    @app.route("/admin/tutors/<int:user_id>/status", methods=["POST"])
    @writes
    def tutor_status(request, user_id):
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        if tutor is None:
            raise HttpError(404, "Unknown tutor.")
        target_type = request.get("target_type", "")
        target_id = request.get_int("target_id")
        action = request.get("action", "")
        reason = request.get("reason", "").strip()
        if target_type not in ("stage", "component", "sub_item") or not target_id:
            raise HttpError(400, "Nothing to update.")
        if action == "approve":
            progress.admin_set_status(request.user, tutor, target_type, target_id,
                                      "completed")
            message = "Marked complete."
        elif action == "reject":
            if not reason:
                request.flash("Give the tutor a reason so they know what to fix.",
                              "error")
                return back_to(request, "/admin/tutors/%d" % user_id)
            progress.admin_set_status(request.user, tutor, target_type, target_id,
                                      "rejected", reason)
            message = "Sent back with your note."
        elif action == "reset":
            progress.admin_reset(request.user, tutor, target_type, target_id)
            message = "Reset to not started."
        else:
            raise HttpError(400, "Unknown action.")
        audit.record(request, "tutor.%s" % action, "user", user_id,
                     "%s %s #%d%s" % (action.title(), target_type, target_id,
                                      (" — %s" % reason) if reason else ""))
        request.flash(message, "ok")
        return back_to(request, "/admin/tutors/%d" % user_id)

    @app.route("/admin/tutors/<int:user_id>/invite", methods=["POST"])
    @writes
    def tutor_invite(request, user_id):
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        session_id = request.get_int("session_id")
        if tutor is None or not session_id:
            raise HttpError(400, "Pick a session first.")
        progress.invite_to_orientation(tutor, session_id)
        audit.record(request, "tutor.invite", "user", user_id,
                     "Invited to session #%d" % session_id)
        request.flash("Invite sent.", "ok")
        return back_to(request, "/admin/tutors/%d" % user_id)

    @app.route("/admin/tutors/<int:user_id>/region", methods=["POST"])
    @writes
    def tutor_region(request, user_id):
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        if tutor is None:
            raise HttpError(404, "Unknown tutor.")
        region_id = request.get_int("region_id")
        db.update("users", user_id, {"region_id": region_id})
        audit.record(request, "tutor.region", "user", user_id,
                     "Region set to %s" % content.region_name(region_id),
                     before={"region_id": tutor.region_id},
                     after={"region_id": region_id})
        progress.sync(wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,))),
                      notifications=False)
        request.flash("Region updated — region-specific training refreshed.", "ok")
        return back_to(request, "/admin/tutors/%d" % user_id)

    @app.route("/admin/tutors/<int:user_id>/grade-cohort", methods=["POST"])
    @writes
    def tutor_grade_cohort(request, user_id):
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        if tutor is None:
            raise HttpError(404, "Unknown tutor.")
        grade_cohort_id = request.get_int("grade_cohort_id")
        db.update("users", user_id, {"grade_cohort_id": grade_cohort_id})
        audit.record(request, "tutor.grade_cohort", "user", user_id,
                     "Grade cohort set to %s"
                     % content.grade_cohort_name(grade_cohort_id),
                     before={"grade_cohort_id": tutor.grade_cohort_id},
                     after={"grade_cohort_id": grade_cohort_id})
        progress.sync(wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,))),
                      notifications=False)
        request.flash("Grade cohort updated — training refreshed.", "ok")
        return back_to(request, "/admin/tutors/%d" % user_id)

    @app.route("/admin/tutors/<int:user_id>/captain", methods=["POST"])
    @writes
    def tutor_captain(request, user_id):
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        if tutor is None:
            raise HttpError(404, "Unknown tutor.")
        captain_id = request.get_int("captain_id")
        db.update("users", user_id, {"captain_id": captain_id})
        audit.record(request, "tutor.captain", "user", user_id,
                     "Activation Director set to %s" % content.captain_name(captain_id),
                     before={"captain_id": tutor.captain_id},
                     after={"captain_id": captain_id})
        request.flash("Activation Director updated.", "ok")
        return back_to(request, "/admin/tutors/%d" % user_id)

    # ------------------------------------------------- bulk attendance CSV -- #
    @app.route("/admin/attendance/import", methods=["GET", "POST"])
    @admin_required
    def attendance_import(request):
        if request.method == "GET":
            return render(request, "admin/attendance_import.html",
                          sessions=wrap_all(db.query(
                              "SELECT * FROM orientation_sessions "
                              "WHERE is_active = 1 ORDER BY id DESC")),
                          result=None)
        request.verify_csrf()
        from .auth import can_write
        if not can_write(request):
            raise HttpError(403, "Your account has read-only access.")
        upload = request.file("file")
        if not upload:
            request.flash("Choose a CSV file to upload.", "error")
            return redirect("/admin/attendance/import")
        session_id = request.get_int("session_id")
        try:
            text = upload.data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = upload.data.decode("latin-1")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any((c or "").strip() for c in r)]
        if not rows:
            request.flash("That file looks empty.", "error")
            return redirect("/admin/attendance/import")

        header = [c.strip().lower() for c in rows[0]]
        known = {"email", "phone", "mobile", "name", "attended", "attendance"}
        has_header = bool(known & set(header))
        data_rows = rows[1:] if has_header else rows

        def column(names):
            for name in names:
                if name in header:
                    return header.index(name)
            return None

        idx_email = column(["email", "email address"]) if has_header else 0
        idx_phone = column(["phone", "mobile", "phone number"]) if has_header else None
        idx_name = column(["name", "tutor", "tutor name"]) if has_header else None
        idx_flag = column(["attended", "attendance", "present"]) if has_header else None

        matched, missing, updated = [], [], 0
        for row in data_rows:
            def cell(index):
                if index is None or index >= len(row):
                    return ""
                return (row[index] or "").strip()

            identifier = cell(idx_email) or cell(idx_phone)
            tutor = None
            if identifier:
                tutor = security.find_user_by_identifier(identifier)
            if tutor is None and cell(idx_name):
                candidates = db.query(
                    "SELECT * FROM users WHERE role_key = 'tutor' "
                    "AND LOWER(name) = LOWER(?)", (cell(idx_name),))
                if len(candidates) == 1:
                    tutor = candidates[0]
            if tutor is None or tutor["role_key"] != "tutor":
                missing.append(identifier or cell(idx_name) or " ".join(row)[:40])
                continue
            flag = cell(idx_flag).lower()
            attended = True if idx_flag is None else flag not in (
                "0", "no", "n", "false", "absent", "")
            progress.set_orientation_attendance(request.user, wrap(tutor), attended,
                                                session_id, source="csv")
            matched.append("%s — %s" % (tutor["name"],
                                        "attended" if attended else "absent"))
            updated += 1
        audit.record(request, "tutor.attendance_import", "orientation_session",
                     session_id,
                     "CSV import: %d updated, %d unmatched" % (updated, len(missing)))
        return render(request, "admin/attendance_import.html",
                      sessions=wrap_all(db.query(
                          "SELECT * FROM orientation_sessions WHERE is_active = 1 "
                          "ORDER BY id DESC")),
                      result={"updated": updated, "matched": matched[:50],
                              "missing": missing[:50],
                              "missing_count": len(missing)})

    # ==================================================================== #
    # Team (admins & viewers)
    # ==================================================================== #

    @app.route("/admin/team")
    @admin_required
    def team_page(request):
        rows = wrap_all(db.query(
            "SELECT u.*, r.name AS role_name FROM users u "
            "JOIN roles r ON r.key = u.role_key "
            "WHERE u.role_key != 'tutor' ORDER BY u.name"))
        roles = wrap_all(db.query("SELECT * FROM roles WHERE can_admin = 1 "
                                  "ORDER BY can_write DESC, key"))
        return render(request, "admin/team.html", team=rows, roles=roles)

    @app.route("/admin/team/new", methods=["POST"])
    @writes
    def team_new(request):
        name = request.get("name", "").strip()
        email = security.normalise_email(request.get("email", ""))
        role_key = request.get("role_key", "viewer")
        password = request.get("password", "")
        role = db.one("SELECT * FROM roles WHERE key = ? AND can_admin = 1",
                      (role_key,))
        problems = []
        if len(name) < 2:
            problems.append("Enter the person's name.")
        if not security.EMAIL_RE.match(email):
            problems.append("Enter a valid work email address.")
        if role is None:
            problems.append("Pick a valid admin role.")
        problem = security.password_problem(password)
        if problem:
            problems.append(problem)
        if db.one("SELECT 1 FROM users WHERE email = ?", (email,)):
            problems.append("Someone already has that email.")
        if problems:
            for message in problems:
                request.flash(message, "error")
            return redirect("/admin/team")
        user_id = db.insert("users", {
            "name": name, "email": email, "phone": None,
            "password_hash": security.hash_password(password),
            "role_key": role_key, "region_id": None, "created_at": db.now(),
        })
        audit.record(request, "team.create", "user", user_id,
                     "Added %s as %s" % (name, role_key))
        request.flash("%s can now sign in at /admin/login." % name, "ok")
        return redirect("/admin/team")

    @app.route("/admin/team/<int:user_id>/role", methods=["POST"])
    @writes
    def team_role(request, user_id):
        row = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None or row["role_key"] == "tutor":
            raise HttpError(404, "Unknown team member.")
        if user_id == request.user["id"]:
            request.flash("You can't change your own access.", "error")
            return redirect("/admin/team")
        values = {}
        role_key = request.get("role_key", "")
        if db.one("SELECT 1 FROM roles WHERE key = ? AND can_admin = 1", (role_key,)):
            values["role_key"] = role_key
        if request.get("is_active", "") != "":
            values["is_active"] = 1 if request.checked("is_active") else 0
        if values:
            db.update("users", user_id, values)
            audit.record(request, "team.update", "user", user_id,
                         "Updated access for %s" % row["name"],
                         before=dict(row), after=values)
            request.flash("Access updated.", "ok")
        return redirect("/admin/team")

    # ==================================================================== #
    # Reporting & audit
    # ==================================================================== #

    @app.route("/admin/reports")
    @admin_required
    def reports_page(request):
        funnel = progress.funnel()
        by_region = []
        for region in content.regions(active_only=False):
            tutors = progress.tutors(region_id=region.id)
            done = sum(1 for t in tutors if t.summary.all_complete)
            avg = (sum(t.summary.percent for t in tutors) / len(tutors)
                   if tutors else 0)
            by_region.append(util.AttrDict({
                "name": region.name, "total": len(tutors), "complete": done,
                "avg_percent": int(round(avg))}))
        policy_stats = wrap_all(db.query(
            "SELECT si.title, COUNT(DISTINCT a.user_id) AS acks "
            "FROM sub_items si LEFT JOIN policy_acknowledgements a "
            "  ON a.sub_item_id = si.id "
            "WHERE si.kind = 'policy' AND si.archived_at IS NULL "
            "GROUP BY si.id ORDER BY si.sort_order"))
        return render(request, "admin/reports.html", funnel=funnel,
                      by_region=by_region, policy_stats=policy_stats,
                      active_tutors=db.scalar(
                          "SELECT COUNT(*) FROM users WHERE role_key='tutor' "
                          "AND is_active=1", (), 0))

    @app.route("/admin/audit")
    @admin_required
    def audit_page(request):
        rows = audit.recent(limit=request.get_int("limit", 200) or 200,
                            entity_type=request.get("entity_type", ""),
                            action=request.get("action", ""))
        wrapped = []
        for row in rows:
            item = wrap(row)
            item.diff = audit.pretty(row)
            wrapped.append(item)
        entity_types = [r["entity_type"] for r in db.query(
            "SELECT DISTINCT entity_type FROM audit_log WHERE entity_type != '' "
            "ORDER BY entity_type")]
        return render(request, "admin/audit.html", rows=wrapped,
                      entity_types=entity_types,
                      entity_type=request.get("entity_type", ""),
                      action=request.get("action", ""))

    # ==================================================================== #
    # Settings & copy
    # ==================================================================== #

    EDITABLE_SETTINGS = [
        ("brand_name", "Brand name", "text"),
        ("support_email", "Support email shown to tutors", "text"),
        ("signup_intro", "Sign-up page welcome copy", "textarea"),
        ("dashboard_intro", "Dashboard welcome copy", "textarea"),
        ("certificate_body", "Certificate wording", "textarea"),
        ("certificate_signatory", "Certificate signatory", "text"),
        ("stalled_days", "Days of no activity before a tutor counts as stalled",
         "number"),
        ("require_upload_approval",
         "Require admin approval for tutor uploads (1 = yes, 0 = no)", "number"),
        ("orientation_stage_key",
         "Stage key that Orientation attendance completes", "text"),
        ("class_prep_tips",
         "Prep tips for Class with a Student (one per line)", "textarea"),
    ]

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def settings_page(request):
        if request.method == "POST":
            from .auth import can_write
            request.verify_csrf()
            if not can_write(request):
                raise HttpError(403, "Your account has read-only access.")
            changed = {}
            for key, label, _kind in EDITABLE_SETTINGS:
                if key in request.form:
                    old = db.setting(key, "")
                    new = request.get(key, "").strip()
                    if old != new:
                        db.set_setting(key, new, label)
                        changed[key] = new
            if changed:
                audit.record(request, "settings.update", "settings", None,
                             "Updated %s" % ", ".join(changed),
                             after=changed)
            request.flash("Settings saved.", "ok")
            return redirect("/admin/settings")
        values = [util.AttrDict({"key": key, "label": label, "kind": kind,
                                 "value": db.setting(key, "")})
                  for key, label, kind in EDITABLE_SETTINGS]
        return render(request, "admin/settings.html", settings=values,
                      outbox=wrap_all(notify.pending_emails(25)))

    @app.route("/admin/notifications/sweep", methods=["POST"])
    @writes
    def notifications_sweep(request):
        sent = notify.deadline_sweep()
        audit.record(request, "notify.sweep", "notification", None,
                     "Queued %d deadline reminder(s)" % sent)
        request.flash("Queued %s." % util.plural(sent, "reminder"), "ok")
        return redirect("/admin/settings")
