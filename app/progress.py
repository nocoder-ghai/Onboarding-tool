"""The unlock / completion engine.

Rules, in one place:
  * A sub-item is done when its tutor_progress row says 'completed'. Policies need
    an acknowledgement row; uploads need a submission.
  * A component is done when every *mandatory, region-visible* sub-item is done
    (or when it is explicitly ticked, for components that have no sub-items).
  * A stage is done when every mandatory component is done — or, for stages with
    completion_rule='admin_marked' (Orientation), when an admin marks it.
  * A stage unlocks only once the stage named by unlock_after_stage_id is done.

Derived completions are written back to tutor_progress so that reporting
(time-to-complete, funnel) has real timestamps to work with.
"""

import datetime

from . import content, db, notify, storage, util
from .util import AttrDict, wrap, wrap_all

DONE = "completed"

# The component key that carries the class-with-a-student booking widget.
CLASS_SLOT_COMPONENT_KEY = "actual_class"


class ValidationError(Exception):
    """Raised when an action would violate a completion rule."""


class LockedError(Exception):
    """Raised when a tutor touches a stage that has not unlocked."""


# --------------------------------------------------------------------------- #
# Progress rows
# --------------------------------------------------------------------------- #

def progress_map(user_id):
    rows = db.query("SELECT * FROM tutor_progress WHERE user_id = ?", (user_id,))
    return {(r["target_type"], r["target_id"]): wrap(r) for r in rows}


def get_progress(user_id, target_type, target_id):
    return wrap(db.one(
        "SELECT * FROM tutor_progress WHERE user_id = ? AND target_type = ? "
        "AND target_id = ?", (user_id, target_type, target_id)))


def set_progress(user_id, target_type, target_id, status=None, **fields):
    existing = get_progress(user_id, target_type, target_id)
    values = dict(fields)
    if status:
        values["status"] = status
        if status == DONE:
            values.setdefault("completed_at", db.now())
        elif status == "submitted":
            values.setdefault("submitted_at", db.now())
        if status != DONE:
            values.setdefault("completed_at", None)
    values["updated_at"] = db.now()
    if existing:
        db.update("tutor_progress", existing.id, values)
        return existing.id
    values.setdefault("status", status or "in_progress")
    values.setdefault("started_at", db.now())
    values.update({"user_id": user_id, "target_type": target_type,
                   "target_id": target_id})
    return db.insert("tutor_progress", values)


def clear_progress(user_id, target_type, target_id):
    db.execute("DELETE FROM tutor_progress WHERE user_id = ? AND target_type = ? "
               "AND target_id = ?", (user_id, target_type, target_id))


def touch_activity(user_id):
    db.execute("UPDATE users SET last_activity_at = ? WHERE id = ?",
               (db.now(), user_id))


# --------------------------------------------------------------------------- #
# Content cache (keeps the funnel from re-querying per tutor)
# --------------------------------------------------------------------------- #

class ContentCache:
    def __init__(self):
        self._stages = None
        self._components = {}
        self._leaves = {}

    def stages(self):
        if self._stages is None:
            self._stages = content.stages()
        return self._stages

    def components(self, stage_id, region_id):
        key = (stage_id, region_id)
        if key not in self._components:
            self._components[key] = content.components(stage_id, region_id)
        return self._components[key]

    def leaves(self, component_id, region_id):
        key = (component_id, region_id)
        if key not in self._leaves:
            self._leaves[key] = content.leaf_sub_items(component_id, region_id)
        return self._leaves[key]


# --------------------------------------------------------------------------- #
# State computation (read-only)
# --------------------------------------------------------------------------- #

def _sub_item_state(item, pmap):
    row = pmap.get(("sub_item", item.id))
    status = row.status if row else "pending"
    state = AttrDict(item)
    state.status = status
    state.done = status == DONE
    state.rejected_reason = row.rejected_reason if row else ""
    state.completed_at = row.completed_at if row else None
    state.progress = row
    return state


def component_state(user, comp, pmap, cache=None):
    cache = cache or ContentCache()
    leaves = cache.leaves(comp.id, user["region_id"])
    states = [_sub_item_state(item, pmap) for item in leaves]
    rule = comp.completion_rule
    if rule == "sub_items" and not leaves:
        # Nothing to tick off — fall back to an explicit confirmation.
        rule = "self_marked"

    row = pmap.get(("component", comp.id))
    if rule == "sub_items":
        mandatory = [s for s in states if s.is_mandatory]
        complete = all(s.done for s in mandatory) if mandatory else False
    else:
        complete = bool(row and row.status == DONE)

    done_units = sum(1 for s in states if s.done)
    total_units = len(states)
    if not total_units:
        done_units, total_units = (1 if complete else 0), 1
    elif complete:
        done_units = total_units

    state = AttrDict(comp)
    state.effective_rule = rule
    state.sub_item_states = states
    state.complete = complete
    state.done_units = done_units
    state.total_units = total_units
    state.percent = util.pct(done_units, total_units)
    state.status = DONE if complete else ("in_progress" if done_units else "pending")
    state.awaiting_admin = bool(row and row.status == "submitted")
    state.rejected_reason = row.rejected_reason if row else ""
    state.pending_items = [s for s in states if s.is_mandatory and not s.done]
    return state


def stage_states(user, cache=None, pmap=None):
    """Status of every stage for this tutor. Performs no writes."""
    cache = cache or ContentCache()
    pmap = progress_map(user["id"]) if pmap is None else pmap
    stages = cache.stages()

    computed = []
    for st in stages:
        comps = cache.components(st.id, user["region_id"])
        comp_states = [component_state(user, c, pmap, cache) for c in comps]
        row = pmap.get(("stage", st.id))
        if st.completion_rule == "admin_marked":
            complete = bool(row and row.status == DONE)
            done_units, total_units = (1 if complete else 0), 1
        else:
            mandatory = [c for c in comp_states if c.is_mandatory]
            complete = all(c.complete for c in mandatory) if mandatory else False
            done_units = sum(c.done_units for c in comp_states)
            total_units = sum(c.total_units for c in comp_states) or 1
            if complete:
                done_units = total_units
        state = AttrDict(st)
        state.components = comp_states
        state.complete = complete
        state.done_units = done_units
        state.total_units = total_units
        state.percent = util.pct(done_units, total_units)
        state.progress = row
        state.unlocked_at = row.started_at if row else None
        state.completed_at = row.completed_at if row else None
        state.awaiting_admin = bool(row and row.status == "submitted")
        computed.append(state)

    by_id = {s.id: s for s in computed}
    for state in computed:
        prereq = by_id.get(state.unlock_after_stage_id)
        if state.complete:
            state.status = DONE
        elif prereq is not None and not prereq.complete:
            state.status = "locked"
        else:
            state.status = "available"
        state.blocked_by = prereq.title if prereq is not None else ""
        state.lock_reason = ""
        if state.status == "locked":
            state.lock_reason = state.locked_hint or (
                "Finish %s first." % prereq.title if prereq is not None
                else "Not open yet.")
        # Deadline is measured from the moment the stage unlocked.
        state.due_in_days = None
        state.due_date = None
        if state.deadline_days and state.unlocked_at and state.status == "available":
            elapsed = db.days_since(state.unlocked_at)
            if elapsed is not None:
                state.due_in_days = state.deadline_days - elapsed
            unlocked_ts = db.parse_ts(state.unlocked_at)
            if unlocked_ts is not None:
                due = unlocked_ts + datetime.timedelta(days=state.deadline_days)
                state.due_date = due.isoformat(sep=" ")
        state.overdue = state.due_in_days is not None and state.due_in_days < 0
        pending = []
        for comp in state.components:
            if comp.complete:
                continue
            pending.extend([p.title for p in comp.pending_items] or [comp.title])
        state.pending_titles = pending
    return computed


def overall(states):
    done = sum(s.done_units for s in states)
    total = sum(s.total_units for s in states) or 1
    stages_done = sum(1 for s in states if s.complete)
    current = next((s for s in states if s.status == "available"), None)
    return AttrDict({
        "percent": util.pct(done, total),
        "done_units": done,
        "total_units": total,
        "stages_done": stages_done,
        "stages_total": len(states),
        "current_stage": current,
        "all_complete": bool(states) and all(s.complete for s in states
                                            if s.is_mandatory),
    })


# --------------------------------------------------------------------------- #
# Sync: persist derived completions, fire unlock notifications
# --------------------------------------------------------------------------- #

def sync(user, cache=None, notifications=True):
    """Recompute, persist derived state, and return fresh stage states."""
    cache = cache or ContentCache()
    states = stage_states(user, cache)

    for state in states:
        for comp in state.components:
            if comp.effective_rule == "sub_items" and comp.complete:
                existing = get_progress(user["id"], "component", comp.id)
                if not existing or existing.status != DONE:
                    set_progress(user["id"], "component", comp.id, DONE)
            elif comp.effective_rule == "sub_items" and not comp.complete:
                existing = get_progress(user["id"], "component", comp.id)
                if existing and existing.status == DONE:
                    set_progress(user["id"], "component", comp.id, "in_progress")
        if state.completion_rule != "admin_marked":
            existing = get_progress(user["id"], "stage", state.id)
            if state.complete and (not existing or existing.status != DONE):
                set_progress(user["id"], "stage", state.id, DONE)
            elif not state.complete and existing and existing.status == DONE:
                set_progress(user["id"], "stage", state.id, "in_progress")

    # Open newly available stages and tell the tutor about it.
    for state in states:
        if state.status != "available":
            continue
        if get_progress(user["id"], "stage", state.id):
            continue
        set_progress(user["id"], "stage", state.id, "in_progress")
        if notifications and state.unlock_after_stage_id:
            notify.notify(user, "stage_unlocked", url="/stage/%d" % state.id,
                          kind="unlock", stage=state.title)

    states = stage_states(user, ContentCache())
    summary = overall(states)
    if summary.all_complete and not user["completed_at"]:
        db.execute("UPDATE users SET completed_at = ? WHERE id = ?",
                   (db.now(), user["id"]))
        if notifications:
            notify.notify(user, "journey_complete", url="/complete",
                          kind="approval")
    elif not summary.all_complete and user["completed_at"]:
        db.execute("UPDATE users SET completed_at = NULL WHERE id = ?", (user["id"],))
    return states


def stage_detail(user, stage_id, states=None):
    """One stage, hydrated with agenda, links, documents and sub-item state."""
    states = states or sync(user)
    state = next((s for s in states if s.id == stage_id), None)
    if state is None:
        return None
    region_id = user["region_id"]
    state.agenda = content.agenda_items(state.id)
    state.links = content.links_for("stage", state.id, region_id)
    state.documents = content.documents_for("stage", state.id, region_id)
    state.session = orientation_session_for(user)
    for comp in state.components:
        comp.links = content.links_for("component", comp.id, region_id)
        comp.documents = content.documents_for("component", comp.id, region_id)
        comp.tree = _hydrate_tree(user, comp, region_id)
        if comp.key == CLASS_SLOT_COMPONENT_KEY:
            comp.class_slot = class_slot_for(user)
            comp.open_slots = [] if comp.class_slot else open_class_slots(region_id)
            comp.prep_tips = prep_tips()
    return state


def _hydrate_tree(user, comp, region_id):
    pmap = progress_map(user["id"])
    tree = content.sub_item_tree(comp.id, region_id)
    out = []
    for node in tree:
        node_state = _sub_item_state(node, pmap)
        node_state.children = []
        for child in node.children:
            child_state = _sub_item_state(child, pmap)
            _attach_item_extras(user, child_state, region_id)
            node_state.children.append(child_state)
        _attach_item_extras(user, node_state, region_id)
        node_state.is_group = node.kind == "group" or bool(node_state.children)
        out.append(node_state)
    return out


def hydrate_tree(user, comp, region_id):
    """Public alias — the admin tutor view renders the same tree a tutor sees."""
    return _hydrate_tree(user, comp, region_id)


def _attach_item_extras(user, item, region_id):
    item.links = content.links_for("sub_item", item.id, region_id)
    item.documents = content.documents_for("sub_item", item.id, region_id)
    item.document = item.documents[0] if item.documents else None
    item.acknowledgement = None
    item.submission = None
    if item.kind == "policy":
        item.acknowledgement = wrap(db.one(
            "SELECT * FROM policy_acknowledgements WHERE user_id = ? "
            "AND sub_item_id = ? ORDER BY id DESC LIMIT 1",
            (user["id"], item.id)))
        current = item.document.current if item.document else None
        item.stale_ack = bool(
            item.acknowledgement and current
            and item.acknowledgement.document_version_id != current.id)
    if item.kind == "upload":
        item.submission = wrap(db.one(
            "SELECT * FROM submissions WHERE user_id = ? AND sub_item_id = ? "
            "AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
            (user["id"], item.id)))
    return item


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def stage_for_sub_item(sub_item_id):
    return wrap(db.one(
        "SELECT s.* FROM stages s JOIN components c ON c.stage_id = s.id "
        "JOIN sub_items si ON si.component_id = c.id WHERE si.id = ?",
        (sub_item_id,)))


def assert_stage_open(user, stage_id):
    states = stage_states(user)
    state = next((s for s in states if s.id == stage_id), None)
    if state is None:
        raise LockedError("That stage isn't available.")
    if state.status == "locked":
        raise LockedError(state.lock_reason or "That stage is still locked.")
    return state


def assert_item_actionable(user, item):
    if item.kind == "group":
        raise ValidationError("That's a heading, not a task.")
    if item.archived_at:
        raise ValidationError("That step is no longer part of your journey.")
    if item.region_id and item.region_id != user["region_id"]:
        raise ValidationError("That step isn't part of your region's training.")
    stage = stage_for_sub_item(item.id)
    if stage is None:
        raise ValidationError("That step is not attached to a stage.")
    assert_stage_open(user, stage.id)
    return stage


# --------------------------------------------------------------------------- #
# Tutor actions
# --------------------------------------------------------------------------- #

def _requires_upload_approval():
    return db.setting_int("require_upload_approval", 0) == 1


def toggle_sub_item(user, item, done):
    """Tick or untick a 'task'/'link' sub-item."""
    assert_item_actionable(user, item)
    if item.kind == "policy":
        raise ValidationError("Please open the policy and acknowledge it instead.")
    if item.kind == "upload":
        raise ValidationError("Please upload your file to complete this step.")
    if done:
        set_progress(user["id"], "sub_item", item.id, DONE, rejected_reason="")
    else:
        set_progress(user["id"], "sub_item", item.id, "in_progress")
    touch_activity(user["id"])
    sync(user)


def acknowledge_policy(user, item, ip_address=""):
    assert_item_actionable(user, item)
    if item.kind != "policy":
        raise ValidationError("That step isn't a policy.")
    doc = content.primary_document(item.id, user["region_id"])
    version = doc.current if doc else None
    if version is None:
        raise ValidationError(
            "This policy document hasn't been uploaded yet. Your mentor has been "
            "notified — please check back shortly.")
    db.execute(
        "INSERT OR IGNORE INTO policy_acknowledgements "
        "(user_id, sub_item_id, document_version_id, acknowledged_at, ip_address) "
        "VALUES (?,?,?,?,?)",
        (user["id"], item.id, version.id, db.now(), ip_address))
    set_progress(user["id"], "sub_item", item.id, DONE, rejected_reason="")
    touch_activity(user["id"])
    sync(user)


def submit_quiz(user, item, answers):
    """CFU quiz for a policy. `answers` maps str(question_id) -> str(choice_id).
    Passes only when every question is answered correctly; reattempts are fine."""
    assert_item_actionable(user, item)
    if item.kind != "policy":
        raise ValidationError("That step isn't a policy.")
    questions = content.quiz_questions(item.id)
    if not questions:
        raise ValidationError("No quiz is set up for this policy yet.")
    score = 0
    for q in questions:
        chosen = answers.get(str(q.id))
        correct_id = next((c.id for c in q.choices if c.is_correct), None)
        if chosen and correct_id and str(chosen) == str(correct_id):
            score += 1
    total = len(questions)
    passed = score == total
    db.insert("quiz_attempts", {
        "user_id": user["id"], "sub_item_id": item.id, "score": score,
        "total": total, "passed": 1 if passed else 0, "submitted_at": db.now(),
    })
    if passed:
        doc = content.primary_document(item.id, user["region_id"])
        version = doc.current if doc else None
        if version:
            db.execute(
                "INSERT OR IGNORE INTO policy_acknowledgements "
                "(user_id, sub_item_id, document_version_id, acknowledged_at, "
                "ip_address) VALUES (?,?,?,?,?)",
                (user["id"], item.id, version.id, db.now(), ""))
        set_progress(user["id"], "sub_item", item.id, DONE, rejected_reason="")
        touch_activity(user["id"])
        sync(user)
    return passed, score, total


def submit_upload(user, item, upload):
    assert_item_actionable(user, item)
    if item.kind != "upload":
        raise ValidationError("That step doesn't take a file.")
    accept = (item.accept_mime or "").lower()
    if accept.startswith("video"):
        profile = "video"
    elif accept.startswith("image"):
        profile = "image"
    else:
        profile = "document"
    mime = storage.validate(upload, profile, item.max_upload_mb)
    key = storage.save("submissions/%d" % user["id"],
                       storage.safe_filename(upload.filename), upload.data)
    db.execute("UPDATE submissions SET superseded_at = ? WHERE user_id = ? "
               "AND sub_item_id = ? AND superseded_at IS NULL",
               (db.now(), user["id"], item.id))
    db.insert("submissions", {
        "user_id": user["id"], "sub_item_id": item.id,
        "filename": storage.safe_filename(upload.filename), "storage_key": key,
        "mime_type": mime, "size_bytes": upload.size,
        "status": "submitted" if _requires_upload_approval() else "approved",
        "submitted_at": db.now(),
    })
    status = "submitted" if _requires_upload_approval() else DONE
    set_progress(user["id"], "sub_item", item.id, status, rejected_reason="")
    touch_activity(user["id"])
    sync(user)
    return status


def mark_component(user, comp, done=True):
    """Tick a component that has no sub-items (e.g. 'Actual Class with Student')."""
    assert_stage_open(user, comp.stage_id)
    pmap = progress_map(user["id"])
    state = component_state(user, comp, pmap)
    if state.effective_rule == "admin_marked":
        raise ValidationError(
            "Your mentor marks this step once they've seen it — nothing to do here.")
    if state.effective_rule == "sub_items":
        raise ValidationError(
            "Finish the checklist first: %s."
            % ", ".join(p.title for p in state.pending_items[:4]))
    set_progress(user["id"], "component", comp.id, DONE if done else "in_progress")
    touch_activity(user["id"])
    sync(user)


def assert_stage_completable(user, stage_id):
    """Part 4 validation: never complete a stage with pending sub-items."""
    pmap = progress_map(user["id"])
    states = stage_states(user, pmap=pmap)
    state = next((s for s in states if s.id == stage_id), None)
    if state is None:
        raise ValidationError("Unknown stage.")
    if state.pending_titles:
        raise ValidationError("Still pending: %s."
                              % ", ".join(state.pending_titles[:5]))
    return state


# --------------------------------------------------------------------------- #
# Admin actions
# --------------------------------------------------------------------------- #

def orientation_session_for(user):
    row = db.one(
        "SELECT s.* FROM orientation_sessions s "
        "JOIN orientation_invites i ON i.session_id = s.id "
        "WHERE i.user_id = ? AND s.is_active = 1 ORDER BY i.id DESC LIMIT 1",
        (user["id"],))
    if row is None:
        # Fall back to the next active session for the tutor's region.
        row = db.one(
            "SELECT * FROM orientation_sessions WHERE is_active = 1 "
            "AND (region_id IS NULL OR region_id = ?) "
            "ORDER BY (starts_at IS NULL), starts_at LIMIT 1",
            (user["region_id"],))
    session = wrap(row)
    if session and session["deck_document_id"]:
        session.deck = content.document(session["deck_document_id"])
        if session.deck:
            session.deck.current = content.current_version(session.deck.id)
    elif session:
        session.deck = None
    return session


def invite_to_orientation(user, session_id):
    db.execute("INSERT OR IGNORE INTO orientation_invites "
               "(user_id, session_id, invited_at) VALUES (?,?,?)",
               (user["id"], session_id, db.now()))
    session = wrap(db.one("SELECT * FROM orientation_sessions WHERE id = ?",
                          (session_id,)))
    if session:
        notify.notify(user, "orientation_invite", url="/dashboard", kind="unlock",
                      when=util.fmt_datetime(session.starts_at),
                      link=session.zoom_link or "your dashboard")


def orientation_stage():
    """The stage whose completion means 'attended Orientation'."""
    key = db.setting("orientation_stage_key", "orientation")
    return content.stage_by_key(key) or next(
        (s for s in content.stages() if s.completion_rule == "admin_marked"), None)


def set_orientation_attendance(actor, tutor, attended, session_id=None,
                               source="manual"):
    existing = db.one("SELECT * FROM orientation_attendance WHERE user_id = ?",
                      (tutor["id"],))
    values = {
        "attended": 1 if attended else 0,
        "session_id": session_id,
        "source": source,
        "marked_by": actor["id"] if actor else None,
        "marked_at": db.now(),
    }
    if existing:
        db.update("orientation_attendance", existing["id"], values)
    else:
        values["user_id"] = tutor["id"]
        db.insert("orientation_attendance", values)
    stage = orientation_stage()
    if stage:
        if attended:
            set_progress(tutor["id"], "stage", stage.id, DONE)
        else:
            set_progress(tutor["id"], "stage", stage.id, "in_progress")
    sync(tutor)


def attendance_row(user_id):
    return wrap(db.one("SELECT * FROM orientation_attendance WHERE user_id = ?",
                       (user_id,)))


# --------------------------------------------------------------------------- #
# Class-with-a-student scheduling
# --------------------------------------------------------------------------- #

def prep_tips():
    return [t.strip() for t in db.setting("class_prep_tips", "").split("\n")
            if t.strip()]


def class_slot_for(user):
    """The tutor's own upcoming or completed class, soonest first."""
    return wrap(db.one(
        "SELECT * FROM class_slots WHERE tutor_id = ? "
        "AND status IN ('booked', 'completed') ORDER BY starts_at LIMIT 1",
        (user["id"],)))


def open_class_slots(region_id):
    return wrap_all(db.query(
        "SELECT * FROM class_slots WHERE status = 'open' "
        "AND (region_id IS NULL OR region_id = ?) ORDER BY starts_at",
        (region_id,)))


def book_class_slot(user, slot_id):
    if class_slot_for(user):
        raise ValidationError("You already have a class booked.")
    slot = wrap(db.one("SELECT * FROM class_slots WHERE id = ?", (slot_id,)))
    if slot is None or slot.status != "open":
        raise ValidationError("That slot isn't available any more — pick another.")
    if slot.region_id and slot.region_id != user["region_id"]:
        raise ValidationError("That slot isn't part of your region.")
    db.execute(
        "UPDATE class_slots SET tutor_id = ?, status = 'booked', booked_at = ?, "
        "updated_at = ? WHERE id = ? AND status = 'open'",
        (user["id"], db.now(), db.now(), slot_id))
    fresh = wrap(db.one("SELECT * FROM class_slots WHERE id = ?", (slot_id,)))
    if fresh is None or fresh.tutor_id != user["id"]:
        raise ValidationError("Someone just booked that slot — pick another.")
    notify.notify(user, "class_booked", url="/dashboard", kind="unlock",
                 student=fresh.student_name or "your student",
                 when=util.fmt_datetime(fresh.starts_at))
    touch_activity(user["id"])


def release_class_slot(user, slot_id):
    slot = wrap(db.one("SELECT * FROM class_slots WHERE id = ? AND tutor_id = ?",
                       (slot_id, user["id"])))
    if slot is None or slot.status != "booked":
        raise ValidationError("That isn't your booked slot.")
    db.execute(
        "UPDATE class_slots SET tutor_id = NULL, status = 'open', booked_at = NULL, "
        "updated_at = ? WHERE id = ?", (db.now(), slot_id))
    touch_activity(user["id"])


def admin_set_status(actor, tutor, target_type, target_id, status, reason=""):
    """Approve, reject, or force-complete any node for a tutor."""
    set_progress(tutor["id"], target_type, target_id, status,
                 rejected_reason=reason,
                 reviewed_by=actor["id"] if actor else None)
    if target_type == "component":
        comp = content.component(target_id)
        if comp and comp.key == CLASS_SLOT_COMPONENT_KEY:
            new_status = "completed" if status == DONE else "booked"
            db.execute(
                "UPDATE class_slots SET status = ?, updated_at = ? "
                "WHERE tutor_id = ? AND status IN ('booked', 'completed')",
                (new_status, db.now(), tutor["id"]))
    if target_type == "sub_item":
        sub = content.sub_item(target_id)
        if sub and sub.kind == "upload":
            db.execute(
                "UPDATE submissions SET status = ?, review_notes = ?, reviewed_by = ? "
                "WHERE user_id = ? AND sub_item_id = ? AND superseded_at IS NULL",
                ("approved" if status == DONE else "rejected", reason,
                 actor["id"] if actor else None, tutor["id"], target_id))
        title = sub.title if sub else "your step"
        if status == DONE:
            notify.notify(tutor, "step_approved", url="/dashboard",
                          kind="approval", item=title)
        elif status == "rejected":
            notify.notify(tutor, "step_rejected", url="/dashboard",
                          kind="approval", item=title, reason=reason or
                          "Please review the instructions and try again.")
    sync(tutor)


def admin_reset(actor, tutor, target_type, target_id):
    """Send a step back to square one, discarding its proof of completion."""
    clear_progress(tutor["id"], target_type, target_id)
    if target_type == "sub_item":
        db.execute("DELETE FROM policy_acknowledgements WHERE user_id = ? "
                   "AND sub_item_id = ?", (tutor["id"], target_id))
        db.execute("UPDATE submissions SET superseded_at = ? WHERE user_id = ? "
                   "AND sub_item_id = ? AND superseded_at IS NULL",
                   (db.now(), tutor["id"], target_id))
    if target_type == "stage":
        stage = content.stage(target_id)
        if stage and stage.completion_rule == "admin_marked":
            db.execute("DELETE FROM orientation_attendance WHERE user_id = ?",
                       (tutor["id"],))
    sync(tutor)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def tutors(region_id=None, stage_id=None, status=None, search="",
           stalled_days=None):
    sql = ["SELECT * FROM users WHERE role_key = 'tutor'"]
    args = []
    if region_id:
        sql.append("AND region_id = ?")
        args.append(region_id)
    if search:
        sql.append("AND (name LIKE ? OR email LIKE ? OR phone LIKE ?)")
        args.extend(["%%%s%%" % search] * 3)
    sql.append("ORDER BY name")
    rows = wrap_all(db.query(" ".join(sql), args))

    cache = ContentCache()
    out = []
    for tutor in rows:
        states = stage_states(tutor, cache)
        summary = overall(states)
        tutor.states = states
        tutor.summary = summary
        tutor.current_stage_title = (summary.current_stage.title
                                     if summary.current_stage else
                                     ("Complete" if summary.all_complete
                                      else "Not started"))
        tutor.current_stage_id = (summary.current_stage.id
                                  if summary.current_stage else None)
        tutor.stalled_days = db.days_since(
            tutor.last_activity_at or tutor.created_at)
        if stage_id and tutor.current_stage_id != stage_id:
            continue
        if status == "complete" and not summary.all_complete:
            continue
        if status == "in_progress" and summary.all_complete:
            continue
        if stalled_days is not None and (tutor.stalled_days is None
                                         or tutor.stalled_days < stalled_days
                                         or summary.all_complete):
            continue
        out.append(tutor)
    return out


def funnel():
    """Per-stage reach/completion counts plus drop-off from the previous stage."""
    cache = ContentCache()
    stages = cache.stages()
    reached = {s.id: 0 for s in stages}
    completed = {s.id: 0 for s in stages}
    tutor_rows = wrap_all(db.query(
        "SELECT * FROM users WHERE role_key = 'tutor' AND is_active = 1"))
    for tutor in tutor_rows:
        for state in stage_states(tutor, cache):
            if state.status in ("available", DONE):
                reached[state.id] += 1
            if state.complete:
                completed[state.id] += 1
    out = []
    previous_completed = None
    for st in stages:
        row = AttrDict(st)
        row.reached = reached[st.id]
        row.completed = completed[st.id]
        row.in_progress = max(reached[st.id] - completed[st.id], 0)
        row.completion_pct = util.pct(completed[st.id], reached[st.id])
        row.drop_off = (max(previous_completed - reached[st.id], 0)
                        if previous_completed is not None else 0)
        row.avg_days = avg_days_for_stage(st.id)
        out.append(row)
        previous_completed = completed[st.id]
    return AttrDict({"stages": out, "total_tutors": len(tutor_rows)})


def avg_days_for_stage(stage_id):
    rows = db.query(
        "SELECT started_at, completed_at FROM tutor_progress "
        "WHERE target_type = 'stage' AND target_id = ? AND status = 'completed' "
        "AND started_at IS NOT NULL AND completed_at IS NOT NULL", (stage_id,))
    spans = []
    for row in rows:
        start, end = db.parse_ts(row["started_at"]), db.parse_ts(row["completed_at"])
        if start and end and end >= start:
            spans.append((end - start).total_seconds() / 86400.0)
    if not spans:
        return None
    return round(sum(spans) / len(spans), 1)


def acknowledgements_for(user_id):
    return wrap_all(db.query(
        "SELECT a.*, si.title AS item_title, d.title AS doc_title, "
        "       dv.version_no, dv.filename "
        "FROM policy_acknowledgements a "
        "JOIN sub_items si ON si.id = a.sub_item_id "
        "LEFT JOIN document_versions dv ON dv.id = a.document_version_id "
        "LEFT JOIN documents d ON d.id = dv.document_id "
        "WHERE a.user_id = ? ORDER BY a.acknowledged_at DESC", (user_id,)))


def submissions_for(user_id, include_superseded=False):
    sql = ("SELECT s.*, si.title AS item_title FROM submissions s "
           "JOIN sub_items si ON si.id = s.sub_item_id WHERE s.user_id = ?")
    if not include_superseded:
        sql += " AND s.superseded_at IS NULL"
    return wrap_all(db.query(sql + " ORDER BY s.id DESC", (user_id,)))
