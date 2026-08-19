"""Small shared helpers, including the dict wrapper templates render against."""

import datetime

from . import db


class AttrDict(dict):
    """Dict with attribute access so templates can write `stage.title`."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


def wrap(row):
    if row is None:
        return None
    return row if isinstance(row, AttrDict) else AttrDict(row)


def wrap_all(rows):
    return [wrap(r) for r in rows or []]


# --------------------------------------------------------------------------- #
# Formatting (registered as template globals)
# --------------------------------------------------------------------------- #

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(value):
    ts = db.parse_ts(value)
    if ts is None:
        return "—"
    return "%d %s %d" % (ts.day, _MONTHS[ts.month - 1], ts.year)


def fmt_datetime(value):
    ts = db.parse_ts(value)
    if ts is None:
        return "—"
    hour = ts.hour % 12 or 12
    ampm = "am" if ts.hour < 12 else "pm"
    return "%d %s %d, %d:%02d %s" % (ts.day, _MONTHS[ts.month - 1], ts.year,
                                     hour, ts.minute, ampm)


def ago(value):
    ts = db.parse_ts(value)
    if ts is None:
        return "never"
    delta = datetime.datetime.utcnow() - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        return "%d hr ago" % (seconds // 3600)
    if delta.days == 1:
        return "yesterday"
    if delta.days < 30:
        return "%d days ago" % delta.days
    return fmt_date(value)


def pct(done, total):
    if not total:
        return 0
    return int(round(100.0 * done / total))


def plural(count, singular, many=None):
    return "%d %s" % (count, singular if count == 1 else (many or singular + "s"))


def truncate(text, limit=90):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def nl2br(text):
    """Escape then convert newlines — safe to use with `|safe` in templates."""
    import html
    return html.escape(str(text or ""), quote=True).replace("\n", "<br>")


def slugify(text, fallback="item"):
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return (slug or fallback)[:60]


def unique_key(table, base):
    """Return a `key` not yet used in `table` (keys are unique per table)."""
    candidate = base
    n = 2
    while db.one("SELECT 1 FROM %s WHERE key = ?" % table, (candidate,)):
        candidate = "%s_%d" % (base, n)
        n += 1
    return candidate


def csv_bytes(header, rows):
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if c is None else c for c in row])
    return buf.getvalue().encode("utf-8-sig")


def _ics_escape(text):
    return (str(text or "").replace("\\", "\\\\").replace(",", "\\,")
            .replace(";", "\\;").replace("\n", "\\n"))


def ics_event(uid, summary, description, starts_at, duration_minutes, location=""):
    """A single-event .ics file, built by hand (no calendar library needed)."""
    start = db.parse_ts(starts_at) or datetime.datetime.utcnow()
    end = start + datetime.timedelta(minutes=duration_minutes or 60)
    stamp = lambda dt: dt.strftime("%Y%m%dT%H%M%S")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Cuemath//Coach Onboarding//EN",
        "BEGIN:VEVENT",
        "UID:%s" % uid,
        "DTSTAMP:%sZ" % stamp(datetime.datetime.utcnow()),
        "DTSTART:%s" % stamp(start),
        "DTEND:%s" % stamp(end),
        "SUMMARY:%s" % _ics_escape(summary),
        "DESCRIPTION:%s" % _ics_escape(description),
    ]
    if location:
        lines.append("LOCATION:%s" % _ics_escape(location))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


TEMPLATE_GLOBALS = {
    "fmt_date": fmt_date,
    "fmt_datetime": fmt_datetime,
    "ago": ago,
    "pct": pct,
    "plural": plural,
    "truncate": truncate,
    "nl2br": nl2br,
}
