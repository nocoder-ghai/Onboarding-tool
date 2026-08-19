"""Audit trail: who changed what content, when, and what the values were."""

import json

from . import db

# Fields that are noise in a diff.
_SKIP = {"updated_at", "created_at"}


def _clean(mapping):
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        mapping = dict(mapping)
    return {k: v for k, v in mapping.items() if k not in _SKIP}


def record(request, action, entity_type="", entity_id=None, summary="",
           before=None, after=None):
    """Write one audit row. `request` may be None for system/CLI actions."""
    actor_id, actor_label, ip = None, "system", ""
    if request is not None and getattr(request, "user", None):
        actor_id = request.user["id"]
        actor_label = "%s <%s>" % (request.user["name"],
                                   request.user["email"] or request.user["phone"])
        ip = request.client_ip
    before_c, after_c = _clean(before), _clean(after)
    if before_c and after_c:
        # Store only what actually changed, so the log stays readable.
        keys = [k for k in after_c if str(before_c.get(k)) != str(after_c.get(k))]
        before_c = {k: before_c.get(k) for k in keys}
        after_c = {k: after_c.get(k) for k in keys}
    db.insert("audit_log", {
        "actor_user_id": actor_id,
        "actor_label": actor_label,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "summary": summary,
        "before_json": json.dumps(before_c, default=str) if before_c else "",
        "after_json": json.dumps(after_c, default=str) if after_c else "",
        "ip_address": ip,
        "created_at": db.now(),
    })


def recent(limit=200, entity_type="", action="", actor_id=None):
    sql = ["SELECT * FROM audit_log WHERE 1=1"]
    args = []
    if entity_type:
        sql.append("AND entity_type = ?")
        args.append(entity_type)
    if action:
        sql.append("AND action LIKE ?")
        args.append("%" + action + "%")
    if actor_id:
        sql.append("AND actor_user_id = ?")
        args.append(actor_id)
    sql.append("ORDER BY id DESC LIMIT ?")
    args.append(limit)
    return db.query(" ".join(sql), args)


def changes_for(entity_type, entity_id, limit=50):
    return db.query(
        "SELECT * FROM audit_log WHERE entity_type = ? AND entity_id = ? "
        "ORDER BY id DESC LIMIT ?", (entity_type, entity_id, limit))


def pretty(row):
    """Render a stored diff as 'field: old -> new' lines for the audit table."""
    try:
        before = json.loads(row["before_json"] or "{}")
        after = json.loads(row["after_json"] or "{}")
    except ValueError:
        return ""
    lines = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, ""), after.get(key, "")
        old_s = str(old)[:80] if old not in (None, "") else "—"
        new_s = str(new)[:80] if new not in (None, "") else "—"
        lines.append("%s: %s → %s" % (key, old_s, new_s))
    return "\n".join(lines)
