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


def simple_markdown(text):
    """Tiny, safe markdown-ish subset -> HTML, for admin-editable copy fields
    (settings textareas) that want basic structure without a real HTML editor.
    Supports **bold**, '### heading' lines, '* '/'- ' bullet lists, and blank
    lines as paragraph breaks. Escapes everything else — safe to use with
    `|safe` in templates."""
    import html
    import re
    text = html.escape(str(text or ""), quote=True)
    lines, out, paragraph, in_list = text.split("\n"), [], [], False

    def flush_paragraph():
        if paragraph:
            out.append('<p style="margin: 0 0 var(--space-12);">%s</p>'
                       % " ".join(paragraph))
            paragraph.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_paragraph()
            continue
        if line.startswith("### "):
            flush_paragraph()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append('<h3 class="type-heading-xs" style="color: var(--text-primary); '
                       'margin: var(--space-16) 0 var(--space-8);">%s</h3>' % line[4:])
        elif line.startswith("* ") or line.startswith("- "):
            flush_paragraph()
            if not in_list:
                out.append('<ul style="margin: 0 0 var(--space-12); '
                           'padding-left: var(--space-20);">')
                in_list = True
            out.append('<li style="margin-bottom: var(--space-4);">%s</li>' % line[2:])
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            paragraph.append(line)
    flush_paragraph()
    if in_list:
        out.append("</ul>")
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", "\n".join(out))


def drive_file_id(url):
    """Pull the file id out of whatever form of Google Drive link was pasted.

    People paste all of these, so accept them all rather than making the admin
    hand-craft one shape:
      https://drive.google.com/file/d/<ID>/view?usp=sharing
      https://drive.google.com/open?id=<ID>
      https://drive.google.com/uc?export=download&id=<ID>
      https://docs.google.com/document/d/<ID>/edit
      <ID> on its own
    """
    import re
    text = str(url or "").strip()
    if not text:
        return ""
    match = re.search(r"/d/([A-Za-z0-9_-]{10,})", text)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text
    return ""


def drive_image_src(url):
    """A URL that works in an <img> tag. Drive's share links render an HTML
    page, not the image, so they can't be used directly."""
    file_id = drive_file_id(url)
    return ("https://drive.google.com/thumbnail?id=%s&sz=w1600" % file_id
            if file_id else "")


def drive_embed_src(url):
    """Drive only plays video through its own player in an iframe — there is
    no direct stream URL we can put in a <video> tag."""
    file_id = drive_file_id(url)
    return "https://drive.google.com/file/d/%s/preview" % file_id if file_id else ""


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
    "simple_markdown": simple_markdown,
    "drive_file_id": drive_file_id,
    "drive_image_src": drive_image_src,
    "drive_embed_src": drive_embed_src,
}
