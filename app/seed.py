"""Seed the database with the onboarding journey described in Part 1.

Idempotent: rows are matched on `key`, so re-running updates copy without
duplicating structure or wiping tutor progress. Placeholder files are clearly
labelled as placeholders — replace them in Admin → Documents.
"""

import os
import secrets
import struct
import zlib

from . import config, content, db, security, storage

PLACEHOLDER_NOTE = ("PLACEHOLDER — replace this file in Admin → Documents. "
                    "Tutors can see whatever is uploaded here.")


# --------------------------------------------------------------------------- #
# Upsert helpers (match on `key`)
# --------------------------------------------------------------------------- #

def _upsert(table, key, values, timestamps=True):
    row = db.one("SELECT * FROM %s WHERE key = ?" % table, (key,))
    if timestamps:
        values = dict(values, updated_at=db.now())
    if row:
        # Whether something is archived is an admin's decision, not the seed's.
        # Re-running this to refresh copy must not quietly put back a stage or
        # step someone deliberately took out of the journey — use Restore in
        # the admin screens for that.
        values = {k: v for k, v in values.items() if k != "archived_at"}
        db.update(table, row["id"], values)
        return row["id"]
    values = dict(values, key=key)
    if timestamps:
        values["created_at"] = db.now()
    return db.insert(table, values)


def _stage(key, **values):
    return _upsert("stages", key, values)


def _component(key, **values):
    return _upsert("components", key, values)


def _item(key, **values):
    return _upsert("sub_items", key, values)


def _link(key, **values):
    return _upsert("links", key, values)


def _document(key, **values):
    return _upsert("documents", key, values)


def _agenda(stage_id, title, description, order):
    row = db.one("SELECT * FROM agenda_items WHERE stage_id = ? AND title = ?",
                 (stage_id, title))
    values = {"description": description, "sort_order": order, "archived_at": None}
    if row:
        db.update("agenda_items", row["id"], values)
        return row["id"]
    return db.insert("agenda_items", dict(values, stage_id=stage_id, title=title))


def _rename_key(table, old_key, new_key):
    """One-off migration: keep the row (and its history) but give it a new key."""
    row = db.one("SELECT id FROM %s WHERE key = ?" % table, (old_key,))
    if row:
        db.execute("UPDATE %s SET key = ? WHERE id = ?" % table, (new_key, row["id"]))


def _quiz_question(item_key, question, choices, correct_index):
    """Idempotent: skip if this policy already has a question with this text."""
    item_id = db.scalar("SELECT id FROM sub_items WHERE key = ?", (item_key,))
    if not item_id:
        return
    if db.one("SELECT 1 FROM quiz_questions WHERE sub_item_id = ? AND question = ?",
              (item_id, question)):
        return
    q_id = db.insert("quiz_questions", {
        "sub_item_id": item_id, "question": question, "sort_order": 10,
        "created_at": db.now(), "archived_at": None,
    })
    for index, choice in enumerate(choices, start=1):
        db.insert("quiz_choices", {
            "question_id": q_id, "label": choice,
            "is_correct": 1 if index == correct_index else 0,
            "sort_order": index * 10,
        })


def _recording(key, title, sub_item_id, note_lines):
    """A placeholder walkthrough recording — replace with the real video in
    Admin → Documents. Stored as a stand-in PDF since no video ships here."""
    doc_id = _document(key, title=title, description="Screen recording.",
                       kind="video", stage_id=None, component_id=None,
                       sub_item_id=sub_item_id, region_id=None, is_active=1,
                       archived_at=None)
    _attach_version(doc_id, "%s-placeholder.pdf" % key.replace("_", "-"),
                    placeholder_pdf(title, [PLACEHOLDER_NOTE, ""] + note_lines),
                    "application/pdf", PLACEHOLDER_NOTE)
    return doc_id


# --------------------------------------------------------------------------- #
# Placeholder file builders (no third-party libraries)
# --------------------------------------------------------------------------- #

def _pdf_escape(text):
    return (str(text).replace("\\", r"\\").replace("(", r"\(")
            .replace(")", r"\)"))


def placeholder_pdf(title, lines):
    """Build a valid single-page PDF with a title and a few lines of text."""
    content_lines = ["BT /F1 20 Tf 56 780 Td (%s) Tj ET" % _pdf_escape(title)]
    y = 748
    for line in lines:
        content_lines.append("BT /F1 11 Tf 56 %d Td (%s) Tj ET"
                             % (y, _pdf_escape(line)))
        y -= 17
    stream = ("\n".join(content_lines) + "\n").encode("latin-1", "replace")

    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream
        + b"endstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(index).encode() + b" 0 obj" + body + b"endobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (b"trailer<</Size " + str(len(objects) + 1).encode()
            + b"/Root 1 0 R>>\nstartxref\n" + str(xref_at).encode()
            + b"\n%%EOF\n")
    return bytes(out)


def placeholder_png(width=360, height=360):
    """A simple Cuemath-blue avatar placeholder, written without Pillow."""
    rows = []
    cx, cy, radius = width / 2.0, height / 2.35, width / 4.4
    for y in range(height):
        row = bytearray(b"\x00")  # PNG filter type 0
        for x in range(width):
            head = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
            shoulders = y > cy + radius * 1.15 and \
                abs(x - cx) < radius * 1.9 - (height - y) * 0.15
            if head or shoulders:
                row += bytes((0xF0, 0xF4, 0xFF))
            else:
                row += bytes((0x00, 0x5A, 0xFF))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def _attach_version(doc_id, filename, data, mime, notes):
    """Attach a file as version 1 only if the document has no versions yet."""
    if db.scalar("SELECT COUNT(*) FROM document_versions WHERE document_id = ?",
                 (doc_id,), 0):
        return None
    key = storage.save("documents/%d" % doc_id, filename, data)
    return db.insert("document_versions", {
        "document_id": doc_id, "version_no": 1, "filename": filename,
        "storage_key": key, "mime_type": mime, "size_bytes": len(data),
        "effective_from": db.now(), "notes": notes,
        "uploaded_by": None, "created_at": db.now(),
    })


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #

ROLES = [
    ("tutor", "Tutor", "Follows the onboarding journey.", 0, 0),
    ("admin", "Admin", "Manages all content and tutor progress.", 1, 1),
    ("viewer", "Viewer (read-only)",
     "Can see every admin screen but cannot change anything.", 1, 0),
]

REGIONS = [
    ("india", "India"),
    ("us_canada", "US & Canada"),
    ("uk_europe", "UK & Europe"),
    ("middle_east", "Middle East"),
    ("rest_of_world", "Rest of world"),
]

GRADE_COHORTS = [
    ("k_5", "K-5"),
    ("g3_8", "3-8"),
    ("g9_12", "9-12"),
]

SETTINGS = [
    ("brand_name", "Cuemath", "Brand name shown in the header"),
    ("support_email", "", "Support email shown to tutors"),
    ("signup_intro",
     "Welcome! Create your account and we'll walk you through onboarding one "
     "step at a time.", "Sign-up page welcome copy"),
    ("dashboard_intro",
     "🌟 **Welcome to the Cuemath Coach Community!** 🌟\n"
     "\n"
     "**Congratulations on successfully completing your Hiring Training at "
     "MathFit Academy! 🎉** You are now officially a **MathFit Coach**. 🚀\n"
     "\n"
     "Your journey with Cuemath begins here, and we're excited to have you "
     "onboard! 💙\n"
     "\n"
     "### 🛤️ What does your journey look like from here?\n"
     "\n"
     "Your **8-week Coach Journey** is designed to help you confidently "
     "transition into your role as a Cuemath Coach.\n"
     "\n"
     "During these 8 weeks, you will:\n"
     "\n"
     "* 👩‍🏫 **Take classes with students** and gain hands-on teaching "
     "experience.\n"
     "* 🎥 **Have your classes monitored** to help you improve your teaching "
     "and coaching skills.\n"
     "* 📱 **Learn to use the Cuemath Coach App and Cuemath Parent App** "
     "effectively.\n"
     "* 📚 **Understand important policies** such as the Pause & Leave Policy "
     "and Session Service Policy.\n"
     "* 🎯 **Complete key activities and tasks** that will help you become a "
     "confident and successful Cuemath Coach.\n"
     "* 🚀 **Understand your growth path with Cuemath** and explore "
     "opportunities to grow as a Coach.\n"
     "\n"
     "This platform will be your **step-by-step guide throughout your "
     "journey**, helping you understand Cuemath, our processes, tools, "
     "policies, and everything you need to know as a Coach.\n"
     "\n"
     "**We're excited to have you with us. Let's begin your Cuemath "
     "journey! 🚀💙**",
     "Dashboard welcome copy"),
    ("certificate_body",
     "has completed the Cuemath coach onboarding and training programme, "
     "including the coach learning platform, a real class with a student, "
     "app training, Cuemath policy, region-specific training, and growth "
     "training.",
     "Certificate wording"),
    ("certificate_signatory", "Cuemath Tutor Success Team",
     "Certificate signatory"),
    ("stalled_days", "7", "Days of no activity before a tutor counts as stalled"),
    ("require_upload_approval", "0",
     "Require admin approval for tutor uploads (1 = yes, 0 = no)"),
    ("orientation_stage_key", "orientation",
     "Stage key that Orientation attendance completes"),
    ("class_prep_tips",
     "Log in a few minutes early, dressed and set up like it's a real class — "
     "it's just you and the student, no one else on the call.\n"
     "Review the student's grade level and topic before the class.\n"
     "Keep a notebook and pen ready to note down progress.\n"
     "Start with a warm, curious question — not a test.\n"
     "Watch your pace: check understanding before moving on.\n"
     "Close with one specific thing the student did well.\n"
     "The session is recorded — your mentor scores it afterward and shares "
     "feedback with you.",
     "Prep tips for Class with a Student (one per line)"),
]

CUEMATH_POLICIES = [
    ("session_service_policy", "Session Service Policy",
     "How sessions must be delivered — punctuality, session length and the "
     "service standard every tutor is held to."),
    ("pause_leave_policy", "Pause & Leave Policy",
     "How to pause classes or take leave without affecting your students."),
    ("performance_policy", "Performance Policy",
     "The standards you're held to and how performance is reviewed."),
    ("compliance_policy", "Compliance Policy",
     "Data privacy, safeguarding and the rules you must never bend."),
]

GROWTH_POLICIES = [
    ("referral_policy", "Referral Policy",
     "How referrals work and what you earn from them."),
    ("renewal_retention_policy", "Renewal & Retention Policy",
     "Your role in keeping students learning with Cuemath."),
]


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #

def seed(verbose=True, demo=False):
    def say(message):
        if verbose:
            print(message)

    db.init_db()

    # -- roles, regions, settings ------------------------------------------ #
    for key, name, description, can_admin, can_write in ROLES:
        db.execute(
            "INSERT INTO roles (key, name, description, can_admin, can_write) "
            "VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "name=excluded.name, description=excluded.description, "
            "can_admin=excluded.can_admin, can_write=excluded.can_write",
            (key, name, description, can_admin, can_write))

    for index, (key, name) in enumerate(REGIONS, start=1):
        row = db.one("SELECT * FROM regions WHERE key = ?", (key,))
        if row:
            db.update("regions", row["id"], {"name": name})
        else:
            db.insert("regions", {"key": key, "name": name,
                                  "sort_order": index * 10, "is_active": 1})
    regions = {r.key: r.id for r in content.regions()}

    for index, (key, name) in enumerate(GRADE_COHORTS, start=1):
        row = db.one("SELECT * FROM grade_cohorts WHERE key = ?", (key,))
        if row:
            db.update("grade_cohorts", row["id"], {"name": name})
        else:
            db.insert("grade_cohorts", {"key": key, "name": name,
                                        "sort_order": index * 10, "is_active": 1})
    grade_cohorts = {g.key: g.id for g in content.grade_cohorts()}

    for key, value, description in SETTINGS:
        if db.one("SELECT 1 FROM settings WHERE key = ?", (key,)) is None:
            db.set_setting(key, value, description)
    say("Roles, regions, grade cohorts and settings ready.")

    # ================================================================== #
    # Stage 1 — Coach Learning Platform
    # ================================================================== #
    _rename_key("stages", "orientation", "coach_learning_platform")
    stage1 = _stage(
        "coach_learning_platform",
        title="Coach Learning Platform",
        subtitle="Self-paced · a quick walkthrough",
        description="Before anything else, see the platform where you'll "
                    "actually give classes: how a live class looks, the tools "
                    "on screen, and how to join and end a session.",
        locked_hint="",
        sort_order=10,
        is_mandatory=1,
        completion_rule="components",
        unlock_after_stage_id=None,
        deadline_days=None,
        archived_at=None,
    )
    platform_comp = _component(
        "platform_walkthrough", stage_id=stage1,
        title="Platform walkthrough",
        description="The features you'll use every time you teach.",
        sort_order=10, is_mandatory=1, completion_rule="sub_items",
        region_id=None, archived_at=None)
    for index, (key, title, description) in enumerate([
        ("platform_live_class", "The live class screen",
         "Whiteboard, student view, and the tools you'll use mid-class."),
        ("platform_joining", "Joining and starting a class",
         "How a class appears on your schedule and how to start it on time."),
        ("platform_ending", "Ending and marking a class",
         "How to close a class properly so it's recorded correctly."),
    ], start=1):
        item_id = _item(
            key, component_id=platform_comp, parent_id=None, title=title,
            description=description, instructions="Watch the recording, then "
                        "explore the same screen yourself before your first class.",
            kind="task", accept_mime="", max_upload_mb=None,
            sort_order=index * 10, is_mandatory=1, region_id=None,
            archived_at=None)
        _recording("%s_recording" % key, title, item_id, [description])
    say("Stage 1 · Coach Learning Platform seeded.")

    # ================================================================== #
    # Stage 2 — Class & App Training
    # ================================================================== #
    _rename_key("stages", "class_with_student", "class_and_app_training")
    stage2 = _stage(
        "class_and_app_training",
        title="Class & App Training",
        subtitle="Three parts, in any order · due within 2 days",
        description="Your real-life class with a student, the Cuemath Coach "
                    "app, and the Cuemath Parent app — finish all three within "
                    "2 days of unlocking.",
        locked_hint="Finish the platform walkthrough first — this opens "
                    "automatically.",
        sort_order=20,
        is_mandatory=1,
        completion_rule="components",
        unlock_after_stage_id=stage1,
        deadline_days=2,
        archived_at=None,
    )
    _component(
        "actual_class", stage_id=stage2,
        title="Actual Class with the Student",
        description="Teach a real class with a real student — no parent on the "
                    "call, no mentor watching live. The session is recorded, "
                    "scored afterward, and your mentor marks this once feedback "
                    "is in.",
        # Not mandatory — not because it's optional in practice, but because
        # stage completion must not wait on a mentor's review. The tutor still
        # sees it, still books it, and the admin still marks it; it just
        # doesn't hold the rest of the journey shut while it's pending.
        sort_order=10, is_mandatory=0, completion_rule="admin_marked",
        region_id=None, archived_at=None)

    # -- Cuemath Coach App Training ----------------------------------------- #
    tutor_app = _component(
        "tutor_app_training", stage_id=stage2,
        title="Cuemath Coach App Training",
        description="The app you'll live in: how it works, onboarding tasks, "
                    "slots, parent communication and support.",
        sort_order=20, is_mandatory=1, completion_rule="sub_items",
        region_id=None, archived_at=None)
    _link("tutor_app_download", label="Download the Cuemath Coach app",
          url="https://example.com/replace-with-tutor-app-link",
          description="Replace with the real app link in Admin → Links.",
          stage_id=None, component_id=tutor_app, sub_item_id=None,
          region_id=None, sort_order=10, is_active=1)

    # C0 — App walkthrough (recordings)
    download_item = _item(
        "download_tutor_app", component_id=tutor_app, parent_id=None,
        title="Download the Cuemath Coach app",
        description="Get the app on your phone before anything else here.",
        instructions="Search for “Cuemath Coach” on the Play Store or App "
                     "Store, install it, and sign in with your registered email.",
        kind="task", accept_mime="", max_upload_mb=None,
        sort_order=2, is_mandatory=1, region_id=None, archived_at=None)
    _recording("download_tutor_app_recording",
               "How to download the Cuemath Coach app", download_item,
               ["Walks through finding, installing and signing in to the app."])

    task_framework_item = _item(
        "task_framework", component_id=tutor_app, parent_id=None,
        title="What is a Task Framework?",
        description="How your onboarding tasks are structured inside the app.",
        instructions="Watch the recording, then open the Tasks tab in the app to "
                     "see your own list.",
        kind="task", accept_mime="", max_upload_mb=None,
        sort_order=4, is_mandatory=1, region_id=None, archived_at=None)
    _recording("task_framework_recording", "What is a Task Framework?",
               task_framework_item,
               ["Explains how tasks, deadlines and approvals work in the app."])

    # C1 — Tasks to complete
    tasks_group = _item(
        "tutor_app_tasks", component_id=tutor_app, parent_id=None,
        title="Tasks to complete", description="Three set-up tasks inside the app.",
        instructions="", kind="group", accept_mime="", max_upload_mb=None,
        sort_order=10, is_mandatory=1, region_id=None, archived_at=None)
    for index, (key, title, description) in enumerate([
        ("eagreement", "eAgreement",
         "Read and sign your engagement agreement in the app."),
        ("bank_details", "Bank details",
         "Add the account we'll pay you into. Double-check the IFSC/routing "
         "number."),
        ("stories_on_app", "Stories on app",
         "Post your first story so students and parents see an active coach."),
    ], start=1):
        _item(key, component_id=tutor_app, parent_id=tasks_group, title=title,
              description=description, instructions="", kind="task",
              accept_mime="", max_upload_mb=None, sort_order=index * 10,
              is_mandatory=1, region_id=None, archived_at=None)

    # C3 — Slots (scheduling a class)
    slots_item = _item(
        "giving_slots", component_id=tutor_app, parent_id=None,
        title="Scheduling a class (giving slots)",
        description="Publish the hours you're available so students can book you.",
        instructions="Open the app's slot planner, add your weekly availability, "
                     "and save. Keep it honest — students book against it.",
        kind="task", accept_mime="", max_upload_mb=None,
        sort_order=30, is_mandatory=1, region_id=None, archived_at=None)
    _recording("giving_slots_recording", "How to schedule a class (give slots)",
               slots_item, ["Shows the slot planner and how bookings land on it."])

    # C4 — Connecting with the parent
    parent_group = _item(
        "connecting_with_parent", component_id=tutor_app, parent_id=None,
        title="Connecting with the parent",
        description="Parents are your partners. Three tools you'll use every week.",
        instructions="", kind="group", accept_mime="", max_upload_mb=None,
        sort_order=40, is_mandatory=1, region_id=None, archived_at=None)
    parent_items = {}
    for index, (key, title, description) in enumerate([
        ("parent_app_chat", "Cuemath Coach App Chat",
         "When to message, response times, and tone."),
        ("parent_class_summary", "Class Summary",
         "Write a summary a parent can act on in under a minute."),
        ("parent_renewal", "Renewal",
         "How to talk about renewal without it feeling like a sales pitch."),
    ], start=1):
        parent_items[key] = _item(
            key, component_id=tutor_app, parent_id=parent_group, title=title,
            description=description, instructions="", kind="task",
            accept_mime="", max_upload_mb=None, sort_order=index * 10,
            is_mandatory=1, region_id=None, archived_at=None)
    _recording("connecting_with_parent_recording", "How to connect with the parent",
               parent_items["parent_app_chat"],
               ["Covers messaging, class summaries and renewal conversations."])

    # C5 — Help & Support
    help_group = _item(
        "help_support", component_id=tutor_app, parent_id=None,
        title="Help & Support",
        description="Where to go when you're stuck. Add your remaining support "
                    "channels in Admin → Content.",
        instructions="", kind="group", accept_mime="", max_upload_mb=None,
        sort_order=50, is_mandatory=1, region_id=None, archived_at=None)
    cueme = _item("cueme", component_id=tutor_app, parent_id=help_group,
                  title="CueMe",
                  description="Your first stop for anything about classes, "
                              "payments or students.",
                  instructions="", kind="task", accept_mime="",
                  max_upload_mb=None, sort_order=10, is_mandatory=1,
                  region_id=None, archived_at=None)
    _link("cueme_link", label="Open CueMe",
          url="https://example.com/replace-with-cueme-link",
          description="Replace with the real CueMe link in Admin → Links.",
          stage_id=None, component_id=None, sub_item_id=cueme,
          region_id=None, sort_order=10, is_active=1)
    ticket_item = _item(
        "raising_ticket", component_id=tutor_app, parent_id=help_group,
        title="Raising a ticket",
        description="How to log an issue on the Cuemath Coach app and track it "
                    "through to resolution.",
        instructions="Open Help in the app, choose the closest category, and "
                     "describe what happened — you'll get a ticket number to "
                     "track.",
        kind="task", accept_mime="", max_upload_mb=None,
        sort_order=20, is_mandatory=1, region_id=None, archived_at=None)
    _recording("raising_ticket_recording", "How to raise a ticket", ticket_item,
               ["Walks through opening a ticket and checking its status."])

    # -- Cuemath Parent App Training ---------------------------------------- #
    cueparent = _component(
        "cueparent_app_training", stage_id=stage2,
        title="Cuemath Parent App Training",
        description="See what parents see. Add the detailed breakdown for this "
                    "component in Admin → Content.",
        sort_order=30, is_mandatory=1, completion_rule="self_marked",
        region_id=None, archived_at=None)
    _link("cueparent_app_download", label="Download the Cuemath Parent app",
          url="https://example.com/replace-with-cueparent-app-link",
          description="Replace with the real app link in Admin → Links.",
          stage_id=None, component_id=cueparent, sub_item_id=None,
          region_id=None, sort_order=10, is_active=1)

    say("Stage 2 · Class & App Training seeded (3 components).")

    # ================================================================== #
    # Stage 3 — First Class (absorbs the old standalone Cuemath Policy stage)
    # ================================================================== #
    _rename_key("sub_items", "wh_policy", "session_service_policy")
    _rename_key("documents", "doc_wh_policy", "doc_session_service_policy")
    # The old "Policies" group heading under Coach App Training is now empty —
    # its children moved to this stage and Growth's Retention & Referrals.
    db.execute("DELETE FROM sub_items WHERE key = 'tutor_app_policies' "
               "AND NOT EXISTS (SELECT 1 FROM sub_items c "
               "WHERE c.parent_id = sub_items.id)")

    # Cuemath Policy grows into First Class rather than being replaced by it:
    # renaming the key keeps the same stage row, so every tutor's progress
    # against it — and every policy acknowledgement hanging off its steps —
    # carries over untouched. The stage gains two components in front of the
    # policies it already had.
    _rename_key("stages", "cuemath_policy", "first_class")

    stage_first = _stage(
        "first_class",
        title="First Class",
        subtitle="Everything you need before you teach for real",
        description="Your first class with a real student is close. This stage "
                    "covers what a Cuemath class looks like, what we measure "
                    "once you're teaching, and the policies you're held to.",
        locked_hint="Finish Class & App Training first — this opens "
                    "automatically.",
        sort_order=30,
        is_mandatory=1,
        completion_rule="components",
        unlock_after_stage_id=stage2,
        deadline_days=3,
        archived_at=None,
    )

    # ---- 1. Readiness ------------------------------------------------- #
    readiness_comp = _component(
        "first_class_readiness", stage_id=stage_first,
        title="First class readiness",
        description="What a Cuemath class actually looks like, and how to be "
                    "set up before the student joins.",
        sort_order=10, is_mandatory=1, completion_rule="sub_items",
        region_id=None, archived_at=None)
    READINESS_STEPS = [
        ("fc_walkthrough", "Watch a first class, end to end",
         "A full class from the coach's side — greeting, concept, practice, "
         "close.",
         "Watch the whole thing before you tick this off."),
        ("fc_workspace", "Set up your workspace and writing tablet",
         "A quiet room, good light, headphones, and your tablet paired and "
         "tested.",
         "Do a dry run: join a test call, share your tablet, write a line."),
        ("fc_checklist", "Run the pre-class checklist",
         "Join early, open the lesson, check your audio and your pen before "
         "the student arrives.",
         "Save the checklist somewhere you'll see it before every class."),
        ("fc_parent_view", "See what the parent sees",
         "Parents follow along in the Cuemath Parent App — know what shows up "
         "there during and after your class.",
         "Skim the parent-side view so nothing surprises you."),
    ]
    for index, (key, title, description, instructions) in enumerate(
            READINESS_STEPS, start=1):
        _item(key, component_id=readiness_comp, parent_id=None, title=title,
              description=description, instructions=instructions, kind="task",
              accept_mime="", max_upload_mb=None, sort_order=index * 10,
              is_mandatory=1, region_id=None, archived_at=None)

    fc_video = _document(
        "doc_first_class_walkthrough", title="A first class, end to end",
        description="Watch a full Cuemath class from the coach's side.",
        kind="video", stage_id=None, component_id=None,
        sub_item_id=db.scalar("SELECT id FROM sub_items WHERE key = ?",
                              ("fc_walkthrough",)),
        region_id=None, is_active=1, archived_at=None)
    _attach_version(fc_video, "first-class-walkthrough-placeholder.pdf",
                    placeholder_pdf("A first class, end to end", [
                        PLACEHOLDER_NOTE, "",
                        "Replace this with the class recording (or a photo, if "
                        "the video isn't shot yet) in",
                        "Admin -> Documents -> A first class, end to end.",
                    ]), "application/pdf", PLACEHOLDER_NOTE)

    # ---- 2. Compliance ------------------------------------------------ #
    compliance_comp = _component(
        "compliance_essentials", stage_id=stage_first,
        title="Compliance essentials",
        description="What gets tracked once you're teaching, and how it adds up.",
        sort_order=20, is_mandatory=1, completion_rule="sub_items",
        region_id=None, archived_at=None)
    _item("fc_what_we_track", component_id=compliance_comp, parent_id=None,
          title="What we track, and why",
          description="Five things are logged against your record: a late login "
                      "to a class, a class no-show, a late login to a trial, a "
                      "trial no-show, and acknowledging a trial more than two "
                      "hours late. They exist because a student is waiting on "
                      "the other side of each one.",
          instructions="Read through the five, then tick this off.",
          kind="task", accept_mime="", max_upload_mb=None, sort_order=10,
          is_mandatory=1, region_id=None, archived_at=None)
    _item("fc_score", component_id=compliance_comp, parent_id=None,
          title="How your compliance score works",
          description="You start at 100. Each logged incident costs 15 points, "
                      "counted over a rolling window, and your score puts you "
                      "in a band your Activation Director can see. Nothing is "
                      "logged without a reason being recorded alongside it.",
          instructions="Ask your Activation Director if anything here is "
                       "unclear — better now than after an incident.",
          kind="task", accept_mime="", max_upload_mb=None, sort_order=20,
          is_mandatory=1, region_id=None, archived_at=None)

    def _policy_group(component_id, policies):
        for index, (key, title, description) in enumerate(policies, start=1):
            item_id = _item(
                key, component_id=component_id, parent_id=None, title=title,
                description=description,
                instructions="Open the document, read it end to end, then "
                             "confirm you've understood it.",
                kind="policy", accept_mime="", max_upload_mb=None,
                sort_order=index * 10, is_mandatory=1, region_id=None,
                archived_at=None)
            doc_id = _document(
                "doc_%s" % key, title=title, description=description,
                kind="policy", stage_id=None, component_id=None,
                sub_item_id=item_id, region_id=None, is_active=1,
                archived_at=None)
            _attach_version(doc_id, "%s-placeholder.pdf" % key.replace("_", "-"),
                            placeholder_pdf(title, [
                                PLACEHOLDER_NOTE, "",
                                description, "",
                                "Upload the approved policy document in",
                                "Admin -> Documents -> %s -> Upload new version."
                                % title, "",
                                "Tutors acknowledge the version that is current "
                                "on the day they read it, and that version is "
                                "kept on record.",
                            ]), "application/pdf", PLACEHOLDER_NOTE)

    # ---- 3. Policies (unchanged rows, now the third component here) ---- #
    cuemath_policy_comp = _component(
        "cuemath_policies", stage_id=stage_first,
        title="Cuemath policies",
        description="Read each one and acknowledge it. We record the date, and "
                    "the exact version you read.",
        sort_order=30, is_mandatory=1, completion_rule="sub_items",
        region_id=None, archived_at=None)
    _policy_group(cuemath_policy_comp, CUEMATH_POLICIES)
    say("Stage 3 · First Class seeded (readiness, compliance, %d policies)."
        % len(CUEMATH_POLICIES))

    # ================================================================== #
    # Stage 3 (repurposed) — Region-Specific Training
    # ================================================================== #
    _rename_key("stages", "training", "region_specific_training")
    stage3 = _stage(
        "region_specific_training",
        title="Region-Specific Training",
        subtitle="Due within a day",
        description="Curriculum, pricing and parent expectations differ by "
                    "region. You'll only see your own region's material.",
        locked_hint="Finish Cuemath Policy first — this opens automatically.",
        sort_order=40,
        is_mandatory=1,
        completion_rule="components",
        unlock_after_stage_id=stage_first,
        deadline_days=1,
        archived_at=None,
    )
    region_comp = _component(
        "region_training", stage_id=stage3,
        title="Region-Specific Training",
        description="Curriculum, pricing and parent expectations differ by region. "
                    "You'll only see your own region's material.",
        sort_order=10, is_mandatory=1, completion_rule="sub_items",
        region_id=None, archived_at=None)
    for index, (region_key, region_name) in enumerate(REGIONS, start=1):
        region_id = regions.get(region_key)
        _item("region_training_%s" % region_key,
              component_id=region_comp, parent_id=None,
              title="%s: curriculum & parent expectations" % region_name,
              description="Region-specific training for %s." % region_name,
              instructions="Attach the real material for this region in "
                           "Admin → Content, then tick this off once reviewed.",
              kind="task", accept_mime="", max_upload_mb=None,
              sort_order=index * 10, is_mandatory=1, region_id=region_id,
              archived_at=None)
    say("Stage 3 · Region-Specific Training seeded.")

    # ================================================================== #
    # Stage 5 — Growth
    # ================================================================== #
    stage5 = _stage(
        "growth",
        title="Growth",
        subtitle="Retention, referrals & Learnosity",
        description="How you grow as a coach: helping students stay, growing "
                    "your own referrals, and using Learnosity for practice "
                    "content.",
        locked_hint="Finish Region-Specific Training first — this opens "
                    "automatically.",
        sort_order=50,
        is_mandatory=1,
        completion_rule="components",
        unlock_after_stage_id=stage3,
        deadline_days=7,
        archived_at=None,
    )
    retention_comp = _component(
        "retention_referrals", stage_id=stage5,
        title="Retention & Referrals",
        description="Two policies that shape how you grow: keeping the "
                    "students you have, and bringing in new ones.",
        sort_order=10, is_mandatory=1, completion_rule="sub_items",
        region_id=None, archived_at=None)
    _policy_group(retention_comp, GROWTH_POLICIES)

    learnosity_comp = _component(
        "learnosity", stage_id=stage5,
        title="Learnosity",
        description="Where students practice between classes. Learnosity opens "
                    "through the Leap platform.",
        sort_order=20, is_mandatory=1, completion_rule="self_marked",
        region_id=None, archived_at=None)
    _link("learnosity_leap_link", label="Open Leap (Learnosity)",
          url="https://example.com/replace-with-leap-platform-link",
          description="Replace with the real Leap platform link in Admin → "
                      "Links.",
          stage_id=None, component_id=learnosity_comp, sub_item_id=None,
          region_id=None, sort_order=10, is_active=1)

    # -- CFU quiz: one placeholder question per policy, admins can add more -- #
    _quiz_question(
        "session_service_policy",
        "What does the Session Service Policy expect from every class you deliver?",
        ["Start on time and deliver the full scheduled duration",
         "Start whenever is convenient for you",
         "Shorten the class if you're running late",
         "Skip it if the student doesn't show up on time"], 1)
    _quiz_question(
        "pause_leave_policy",
        "If you need to pause classes or take leave, what should you do first?",
        ["Request it through the proper pause/leave process before taking time off",
         "Just stop showing up for classes",
         "Tell the student directly and skip the app",
         "Wait until someone notices"], 1)
    _quiz_question(
        "compliance_policy",
        "Under the Compliance Policy, how should you handle a student's personal data?",
        ["Keep it private and only use it for coaching purposes",
         "Share it with anyone who asks",
         "Post it on social media to show engagement",
         "Save it on a personal, unsecured device"], 1)
    _quiz_question(
        "performance_policy",
        "What determines how your performance is reviewed under the Performance Policy?",
        ["The standards described in the policy, reviewed regularly",
         "Whatever the tutor personally feels is fair",
         "Nothing — performance is never reviewed",
         "Only what students post on social media"], 1)
    _quiz_question(
        "referral_policy",
        "Under the Referral Policy, when do you earn a referral reward?",
        ["When a referral meets the conditions set out in the policy",
         "Immediately after mentioning Cuemath to anyone",
         "Only if you ask your mentor directly",
         "Referrals are never rewarded"], 1)
    _quiz_question(
        "renewal_retention_policy",
        "What's your role under the Renewal & Retention Policy?",
        ["Help students stay engaged so they keep learning with Cuemath",
         "Renewals are handled entirely by someone else",
         "Push renewal even if the student is unhappy",
         "Ignore renewal conversations entirely"], 1)

    say("Stage 5 · Growth seeded (%d policies, Learnosity)."
        % len(GROWTH_POLICIES))

    # ================================================================== #
    # Stage 6 — Class Quality (Instructional Review)
    # ================================================================== #
    stage6 = _stage(
        "class_quality",
        title="Class Quality",
        subtitle="How your classes get reviewed",
        description="Every class you teach is reviewed through our IR tool, "
                    "and a report is shared with you. This step is "
                    "informational — read through how it works and the "
                    "parameters you're reviewed on.",
        locked_hint="Finish Growth first — this opens automatically.",
        sort_order=60,
        is_mandatory=1,
        completion_rule="components",
        unlock_after_stage_id=stage5,
        deadline_days=None,
        archived_at=None,
    )
    ir_comp = _component(
        "instructional_review", stage_id=stage6,
        title="How Instructional Review works",
        description="The IR tool reviews a sample of your classes against a "
                    "set of teaching parameters, then shares a report with "
                    "you.",
        sort_order=10, is_mandatory=1, completion_rule="self_marked",
        region_id=None, archived_at=None)
    _link("ir_tool_link", label="Open the IR tool",
          url="https://example.com/replace-with-ir-tool-link",
          description="Replace with the real IR tool link in Admin → Links.",
          stage_id=None, component_id=ir_comp, sub_item_id=None,
          region_id=None, sort_order=10, is_active=1)
    ir_guide = _document(
        "ir_parameters_guide", title="Instructional Review parameters",
        description="What each class is scored on.",
        kind="guide", stage_id=None, component_id=ir_comp, sub_item_id=None,
        region_id=None, is_active=1, archived_at=None)
    _attach_version(ir_guide, "ir-parameters-placeholder.pdf",
                    placeholder_pdf("Instructional Review parameters", [
                        PLACEHOLDER_NOTE, "",
                        "Typical parameters (replace with the real rubric):",
                        "  1. Opening & rapport",
                        "  2. Concept clarity",
                        "  3. Pace & checks for understanding",
                        "  4. Practice & independence",
                        "  5. Closing & summary",
                    ]), "application/pdf", PLACEHOLDER_NOTE)
    say("Stage 6 · Class Quality (Instructional Review) seeded.")

    admin = ensure_admin(verbose=verbose)
    if demo:
        _seed_demo_tutors(regions, say)
    return admin


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

def ensure_admin(email=None, password=None, name="Cuemath Admin", verbose=True):
    """Create the first admin if none exists. Returns (email, password|None)."""
    email = security.normalise_email(
        email or os.environ.get("CUEMATH_ADMIN_EMAIL") or "admin@cuemath.com")
    existing = db.one("SELECT * FROM users WHERE email = ?", (email,))
    any_admin = db.one("SELECT 1 FROM users u JOIN roles r ON r.key = u.role_key "
                       "WHERE r.can_write = 1 AND r.can_admin = 1")
    if existing or any_admin:
        return (email, None)
    password = password or os.environ.get("CUEMATH_ADMIN_PASSWORD") \
        or ("cue" + secrets.token_urlsafe(9))
    db.insert("users", {
        "name": name, "email": email, "phone": None,
        "password_hash": security.hash_password(password),
        "role_key": "admin", "region_id": None, "created_at": db.now(),
    })
    if verbose:
        print("\n  Admin account created")
        print("    URL:      /admin/login")
        print("    Email:    %s" % email)
        print("    Password: %s" % password)
        print("    Change this password after your first sign-in.\n")
    return (email, password)


def _seed_demo_tutors(regions, say):
    """Three tutors at different points, so the funnel and filters have data."""
    from . import progress
    from .util import wrap

    demo = [
        ("Ananya Rao", "ananya.demo@example.com", "india", "orientation"),
        ("Marcus Bell", "marcus.demo@example.com", "us_canada", "profile"),
        ("Leila Haddad", "leila.demo@example.com", "middle_east", "training"),
    ]
    created = 0
    for name, email, region_key, reached in demo:
        if db.one("SELECT 1 FROM users WHERE email = ?", (email,)):
            continue
        user_id = db.insert("users", {
            "name": name, "email": email, "phone": None,
            "password_hash": security.hash_password("demopass123"),
            "role_key": "tutor", "region_id": regions.get(region_key),
            "created_at": db.now(), "last_activity_at": db.now(),
        })
        tutor = wrap(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))
        progress.sync(tutor, notifications=False)
        if reached in ("profile", "training"):
            # Finish the Coach Learning Platform walkthrough — unlocks Stage 2.
            platform = content.component(db.scalar(
                "SELECT id FROM components WHERE key = 'platform_walkthrough'"))
            for item in content.leaf_sub_items(platform.id, tutor.region_id):
                progress.set_progress(tutor.id, "sub_item", item.id, "completed")
            progress.sync(tutor, notifications=False)
        if reached == "training":
            # Finish all of Class & App Training — unlocks Cuemath Policy.
            actual_class = content.component(db.scalar(
                "SELECT id FROM components WHERE key = 'actual_class'"))
            progress.set_progress(tutor.id, "component", actual_class.id, "completed")
            tutor_app = content.component(db.scalar(
                "SELECT id FROM components WHERE key = 'tutor_app_training'"))
            for item in content.leaf_sub_items(tutor_app.id, tutor.region_id):
                progress.set_progress(tutor.id, "sub_item", item.id, "completed")
            cueparent = content.component(db.scalar(
                "SELECT id FROM components WHERE key = 'cueparent_app_training'"))
            progress.set_progress(tutor.id, "component", cueparent.id, "completed")
            progress.sync(tutor, notifications=False)
        created += 1
    if created:
        say("Seeded %d demo tutor(s) — password: demopass123" % created)
