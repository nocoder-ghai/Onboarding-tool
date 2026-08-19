"""In-app notifications plus a queued email outbox.

Email is written to `email_outbox` instead of being sent inline: no SMTP
credentials are needed to run the app, and production can drain the table with a
worker (see README > Email).
"""

import datetime

from . import config, db
from .util import wrap_all

# Tutor-facing copy lives here (and in `settings`) so it stays warm and coach-like.
TEMPLATES = {
    "stage_unlocked": (
        "{stage} is open 🎉",
        "Nice work, {name}! You've finished the previous step, so {stage} is now "
        "unlocked. Head to your dashboard when you're ready — it should take about "
        "a few focused minutes.",
    ),
    "deadline_soon": (
        "{stage} is due in {days} day(s)",
        "Hi {name}, just a friendly nudge — {stage} is due in {days} day(s). "
        "You're closer than you think. Open your dashboard to pick up where you "
        "left off.",
    ),
    "step_approved": (
        "{item} approved ✅",
        "Great job, {name} — your {item} has been approved. Onwards!",
    ),
    "step_rejected": (
        "{item} needs another look",
        "Hi {name}, your {item} needs a small change before we can approve it:\n\n"
        "{reason}\n\nYou can resubmit straight from your dashboard — you've got this.",
    ),
    "journey_complete": (
        "You're fully onboarded 🎓",
        "Congratulations {name}! You've completed every stage of Cuemath tutor "
        "onboarding. Your certificate is waiting on your dashboard.",
    ),
    "orientation_invite": (
        "You're invited to Orientation",
        "Welcome aboard, {name}! Your Orientation call is {when}. Join here: {link}\n\n"
        "Come curious — we'll cover the coach mindset, how you'll be evaluated, and "
        "how to build a profile parents trust.",
    ),
    "class_booked": (
        "Your class with {student} is booked",
        "Nice, {name}! You're booked in for {when}. Check your dashboard for a few "
        "quick prep tips before the session — you've got this.",
    ),
}


def notify(user, key, url="", kind="info", email=True, **fields):
    """Create an in-app notification and (optionally) queue the same as email."""
    if user is None:
        return
    subject_tpl, body_tpl = TEMPLATES.get(key, ("{title}", "{body}"))
    data = {"name": (user["name"] or "there").split(" ")[0]}
    data.update({k: ("" if v is None else v) for k, v in fields.items()})
    try:
        subject = subject_tpl.format(**data)
        body = body_tpl.format(**data)
    except KeyError:
        subject, body = key.replace("_", " ").title(), ""
    db.insert("notifications", {
        "user_id": user["id"], "title": subject, "body": body, "url": url,
        "kind": kind, "created_at": db.now(),
    })
    if email and user["email"]:
        queue_email(user["email"], subject, body)


def queue_email(to_address, subject, body):
    db.insert("email_outbox", {
        "to_address": to_address, "subject": subject, "body": body,
        "created_at": db.now(),
    })


def send_otp(identifier, code, kind):
    """Deliver a login code. In dev this prints to the console."""
    subject = "Your Cuemath onboarding code"
    body = ("Your login code is %s. It expires in %d minutes.\n\n"
            "If you didn't ask for this, you can ignore this message."
            % (code, config.OTP_TTL_SECONDS // 60))
    if kind == "email":
        queue_email(identifier, subject, body)
    if config.PRINT_OTP:
        print("[OTP] %s -> %s" % (identifier, code))


def unread_count(user_id):
    return db.scalar("SELECT COUNT(*) FROM notifications "
                     "WHERE user_id = ? AND read_at IS NULL", (user_id,), 0)


def for_user(user_id, limit=40):
    return wrap_all(db.query("SELECT * FROM notifications WHERE user_id = ? "
                             "ORDER BY id DESC LIMIT ?", (user_id, limit)))


def mark_all_read(user_id):
    db.execute("UPDATE notifications SET read_at = ? "
               "WHERE user_id = ? AND read_at IS NULL", (db.now(), user_id))


def pending_emails(limit=100):
    return wrap_all(db.query("SELECT * FROM email_outbox WHERE sent_at IS NULL "
                             "ORDER BY id LIMIT ?", (limit,)))


def deadline_sweep():
    """Queue reminders for stages whose deadline is close. Idempotent per day:
    a reminder is only sent if none was created for that stage today."""
    from . import progress  # local import avoids a cycle at module load
    sent = 0
    today = datetime.date.today().isoformat()
    tutors = db.query("SELECT * FROM users WHERE role_key = 'tutor' AND is_active = 1 "
                      "AND completed_at IS NULL")
    for tutor in tutors:
        for stage in progress.stage_states(tutor):
            if stage["status"] != "available" or stage["due_in_days"] is None:
                continue
            if stage["due_in_days"] > 2:
                continue
            already = db.one(
                "SELECT 1 FROM notifications WHERE user_id = ? AND kind = 'deadline' "
                "AND title LIKE ? AND created_at >= ?",
                (tutor["id"], "%" + stage["title"][:40] + "%", today))
            if already:
                continue
            notify(tutor, "deadline_soon", url="/stage/%d" % stage["id"],
                   kind="deadline", stage=stage["title"],
                   days=max(stage["due_in_days"], 0))
            sent += 1
    return sent
