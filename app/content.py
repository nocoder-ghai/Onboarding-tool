"""Reads of the admin-managed content tree, with region and grade-cohort
filtering applied.

Everything here answers the question "what should *this* tutor see?" — archived
rows are hidden, and rows tagged with a region and/or grade cohort are only
visible to tutors matching both.
"""

from . import db, util
from .util import wrap, wrap_all

VISIBLE_STAGE = "archived_at IS NULL"


def _region_sql(column="region_id"):
    return "(%s IS NULL OR %s = ?)" % (column, column)


def _scope_sql(region_column="region_id", grade_column="grade_cohort_id"):
    """A pair of NULL-or-matches clauses — content tagged with a region and/or
    grade cohort is only visible to a tutor matching both. Two `?` placeholders,
    bind (region_id, grade_cohort_id) in that order."""
    return "%s AND %s" % (_region_sql(region_column), _region_sql(grade_column))


# --------------------------------------------------------------------------- #
# Regions, grade cohorts & settings
# --------------------------------------------------------------------------- #

def regions(active_only=True):
    sql = "SELECT * FROM regions"
    if active_only:
        sql += " WHERE is_active = 1"
    return wrap_all(db.query(sql + " ORDER BY sort_order, name"))


def region(region_id):
    if not region_id:
        return None
    return wrap(db.one("SELECT * FROM regions WHERE id = ?", (region_id,)))


def region_name(region_id):
    row = region(region_id)
    return row.name if row else "All regions"


def grade_cohorts(active_only=True):
    sql = "SELECT * FROM grade_cohorts"
    if active_only:
        sql += " WHERE is_active = 1"
    return wrap_all(db.query(sql + " ORDER BY sort_order, name"))


def grade_cohort(grade_cohort_id):
    if not grade_cohort_id:
        return None
    return wrap(db.one("SELECT * FROM grade_cohorts WHERE id = ?", (grade_cohort_id,)))


def grade_cohort_name(grade_cohort_id):
    row = grade_cohort(grade_cohort_id)
    return row.name if row else "All grade cohorts"


# --------------------------------------------------------------------------- #
# Captains (admin/viewer accounts that track a subset of tutors)
# --------------------------------------------------------------------------- #

def captains():
    return wrap_all(db.query(
        "SELECT * FROM users WHERE role_key IN ('admin', 'viewer') "
        "AND is_active = 1 ORDER BY name"))


def captain(captain_id):
    """The full Activation Director record — the tutor sidebar shows their
    name, email and phone so a tutor knows who to reach."""
    if not captain_id:
        return None
    return wrap(db.one("SELECT * FROM users WHERE id = ?", (captain_id,)))


def captain_name(captain_id):
    if not captain_id:
        return "Unassigned"
    row = captain(captain_id)
    return row.name if row else "Unassigned"


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #

def stages(include_archived=False):
    sql = "SELECT * FROM stages"
    if not include_archived:
        sql += " WHERE " + VISIBLE_STAGE
    return wrap_all(db.query(sql + " ORDER BY sort_order, id"))


def stage(stage_id):
    return wrap(db.one("SELECT * FROM stages WHERE id = ?", (stage_id,)))


def stage_by_key(key):
    return wrap(db.one("SELECT * FROM stages WHERE key = ?", (key,)))


def agenda_items(stage_id, include_archived=False):
    sql = "SELECT * FROM agenda_items WHERE stage_id = ?"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    return wrap_all(db.query(sql + " ORDER BY sort_order, id", (stage_id,)))


# --------------------------------------------------------------------------- #
# Components & sub-items
# --------------------------------------------------------------------------- #

def components(stage_id, region_id=None, grade_cohort_id=None,
              include_archived=False, all_regions=False):
    sql = ["SELECT * FROM components WHERE stage_id = ?"]
    args = [stage_id]
    if not include_archived:
        sql.append("AND archived_at IS NULL")
    if not all_regions:
        sql.append("AND " + _scope_sql())
        args.extend([region_id, grade_cohort_id])
    sql.append("ORDER BY sort_order, id")
    return wrap_all(db.query(" ".join(sql), args))


def component(component_id):
    return wrap(db.one("SELECT * FROM components WHERE id = ?", (component_id,)))


def sub_items(component_id, region_id=None, grade_cohort_id=None,
              include_archived=False, all_regions=False, parent_id=None,
              top_level_only=True):
    sql = ["SELECT * FROM sub_items WHERE component_id = ?"]
    args = [component_id]
    if parent_id is not None:
        sql.append("AND parent_id = ?")
        args.append(parent_id)
    elif top_level_only:
        sql.append("AND parent_id IS NULL")
    if not include_archived:
        sql.append("AND archived_at IS NULL")
    if not all_regions:
        sql.append("AND " + _scope_sql())
        args.extend([region_id, grade_cohort_id])
    sql.append("ORDER BY sort_order, id")
    return wrap_all(db.query(" ".join(sql), args))


def sub_item(sub_item_id):
    return wrap(db.one("SELECT * FROM sub_items WHERE id = ?", (sub_item_id,)))


def sub_item_tree(component_id, region_id=None, grade_cohort_id=None,
                  include_archived=False, all_regions=False):
    """Top-level sub-items, each with a `children` list (one nesting level)."""
    tree = sub_items(component_id, region_id, grade_cohort_id, include_archived,
                     all_regions)
    for node in tree:
        node.children = sub_items(component_id, region_id, grade_cohort_id,
                                  include_archived, all_regions, parent_id=node.id)
    return tree


def leaf_sub_items(component_id, region_id=None, grade_cohort_id=None):
    """Every completable sub-item under a component ('group' rows excluded)."""
    leaves = []
    for node in sub_item_tree(component_id, region_id, grade_cohort_id):
        children = node.children
        if node.kind == "group" or children:
            leaves.extend([c for c in children if c.kind != "group"])
        else:
            leaves.append(node)
    return leaves


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #

_TARGET_COLUMN = {"stage": "stage_id", "component": "component_id",
                  "sub_item": "sub_item_id"}


def links_for(target_type, target_id, region_id=None, grade_cohort_id=None):
    column = _TARGET_COLUMN[target_type]
    return wrap_all(db.query(
        "SELECT * FROM links WHERE %s = ? AND is_active = 1 AND %s "
        "ORDER BY sort_order, id" % (column, _scope_sql()),
        (target_id, region_id, grade_cohort_id)))


def link_by_key(key):
    return wrap(db.one("SELECT * FROM links WHERE key = ? AND is_active = 1", (key,)))


def global_links(region_id=None, grade_cohort_id=None):
    return wrap_all(db.query(
        "SELECT * FROM links WHERE stage_id IS NULL AND component_id IS NULL "
        "AND sub_item_id IS NULL AND is_active = 1 AND %s "
        "ORDER BY sort_order, id" % _scope_sql(), (region_id, grade_cohort_id)))


def all_links():
    return wrap_all(db.query("SELECT * FROM links ORDER BY sort_order, id"))


# --------------------------------------------------------------------------- #
# Documents & versions
# --------------------------------------------------------------------------- #

def current_version(document_id):
    """Newest version whose effective_from has arrived."""
    return wrap(db.one(
        "SELECT * FROM document_versions WHERE document_id = ? "
        "AND effective_from <= ? ORDER BY effective_from DESC, version_no DESC "
        "LIMIT 1", (document_id, db.now())))


def versions(document_id):
    return wrap_all(db.query(
        "SELECT * FROM document_versions WHERE document_id = ? "
        "ORDER BY version_no DESC", (document_id,)))


def next_version_no(document_id):
    return db.scalar("SELECT COALESCE(MAX(version_no), 0) + 1 FROM document_versions "
                     "WHERE document_id = ?", (document_id,), 1)


def document(document_id):
    return wrap(db.one("SELECT * FROM documents WHERE id = ?", (document_id,)))


def document_by_key(key):
    return wrap(db.one("SELECT * FROM documents WHERE key = ?", (key,)))


def _attach_current(docs):
    for doc in docs:
        doc.current = current_version(doc.id)
        doc.version_count = db.scalar(
            "SELECT COUNT(*) FROM document_versions WHERE document_id = ?",
            (doc.id,), 0)
    return docs


def documents_for(target_type, target_id, region_id=None, grade_cohort_id=None):
    column = _TARGET_COLUMN[target_type]
    docs = wrap_all(db.query(
        "SELECT * FROM documents WHERE %s = ? AND is_active = 1 "
        "AND archived_at IS NULL AND %s ORDER BY id"
        % (column, _scope_sql()), (target_id, region_id, grade_cohort_id)))
    return _attach_current(docs)


def all_documents(include_archived=False):
    sql = "SELECT * FROM documents"
    if not include_archived:
        sql += " WHERE archived_at IS NULL"
    return _attach_current(wrap_all(db.query(sql + " ORDER BY kind, title")))


def primary_document(sub_item_id, region_id=None, grade_cohort_id=None):
    """The document a policy sub-item asks the tutor to read."""
    docs = documents_for("sub_item", sub_item_id, region_id, grade_cohort_id)
    return docs[0] if docs else None


# --------------------------------------------------------------------------- #
# CFU quiz (policy comprehension check)
# --------------------------------------------------------------------------- #

def quiz_questions(sub_item_id, include_archived=False):
    sql = "SELECT * FROM quiz_questions WHERE sub_item_id = ?"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    questions = wrap_all(db.query(sql + " ORDER BY sort_order, id", (sub_item_id,)))
    for q in questions:
        q.choices = wrap_all(db.query(
            "SELECT * FROM quiz_choices WHERE question_id = ? ORDER BY sort_order, id",
            (q.id,)))
    return questions


# --------------------------------------------------------------------------- #
# Ordering helpers (used by the admin reorder buttons)
# --------------------------------------------------------------------------- #

def next_sort_order(table, where_sql="1=1", args=()):
    return db.scalar("SELECT COALESCE(MAX(sort_order), 0) + 10 FROM %s WHERE %s"
                     % (table, where_sql), args, 10)


def move(table, row_id, direction, sibling_sql="1=1", sibling_args=()):
    """Swap sort_order with the adjacent sibling. Returns True if anything moved."""
    row = db.one("SELECT id, sort_order FROM %s WHERE id = ?" % table, (row_id,))
    if row is None:
        return False
    comparator, order = ("<", "DESC") if direction == "up" else (">", "ASC")
    neighbour = db.one(
        "SELECT id, sort_order FROM %s WHERE %s AND id != ? AND "
        "(sort_order %s ? OR (sort_order = ? AND id %s ?)) "
        "ORDER BY sort_order %s, id %s LIMIT 1"
        % (table, sibling_sql, comparator, comparator, order, order),
        tuple(sibling_args) + (row_id, row["sort_order"], row["sort_order"], row_id))
    if neighbour is None:
        return False
    # Equal sort_order values are possible after imports; force a distinct pair.
    a, b = row["sort_order"], neighbour["sort_order"]
    if a == b:
        b = a - 1 if direction == "up" else a + 1
    db.execute("UPDATE %s SET sort_order = ? WHERE id = ?" % table, (b, row_id))
    db.execute("UPDATE %s SET sort_order = ? WHERE id = ?" % table, (a, neighbour["id"]))
    return True


def resequence(table, sibling_sql="1=1", sibling_args=()):
    """Normalise sort_order to 10, 20, 30 … so later inserts land predictably."""
    rows = db.query("SELECT id FROM %s WHERE %s ORDER BY sort_order, id"
                    % (table, sibling_sql), sibling_args)
    for index, row in enumerate(rows, start=1):
        db.execute("UPDATE %s SET sort_order = ? WHERE id = ?" % table,
                   (index * 10, row["id"]))


def unique_key(table, base):
    return util.unique_key(table, base)
