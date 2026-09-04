-- ===========================================================================
-- Cuemath Tutor Onboarding & Training Tool — relational schema
--
-- Design rule: every piece of text, link, document and structural relationship
-- a tutor sees lives in this database and is editable from /admin. The journey
-- structure itself (stages -> components -> sub_items) is data, not code, so
-- admins can add/reorder/archive steps without a deploy.
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- Identity & access
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS roles (
    key         TEXT PRIMARY KEY,           -- 'tutor' | 'admin' | 'viewer'
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    can_admin   INTEGER NOT NULL DEFAULT 0, -- may open /admin at all
    can_write   INTEGER NOT NULL DEFAULT 0  -- may change content/tutor state
);

CREATE TABLE IF NOT EXISTS regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active  INTEGER NOT NULL DEFAULT 1
);

-- Which grades a tutor will teach (e.g. "K-5", "3-8", "9-12"). Gates content
-- the same way regions do — a component/sub-item/link/document tagged with a
-- cohort is only shown to tutors in that cohort.
CREATE TABLE IF NOT EXISTS grade_cohorts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    email            TEXT UNIQUE,
    phone            TEXT UNIQUE,
    -- Unique code identifying a coach across systems (e.g. the Rise
    -- training CSV's Teacher DB ID) and their onboarding journey here.
    db_id            TEXT UNIQUE,
    password_hash    TEXT,                  -- NULL => OTP-only account
    role_key         TEXT NOT NULL REFERENCES roles(key),
    region_id        INTEGER REFERENCES regions(id),
    grade_cohort_id  INTEGER REFERENCES grade_cohorts(id),
    -- for a tutor: which admin/viewer account (a "captain") tracks them.
    -- NULL means unassigned. Ignored for non-tutor users.
    captain_id       INTEGER REFERENCES users(id),
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    last_login_at    TEXT,
    last_activity_at TEXT,
    completed_at     TEXT,                  -- journey fully finished
    CHECK (email IS NOT NULL OR phone IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_users_role    ON users(role_key);
CREATE INDEX IF NOT EXISTS idx_users_region  ON users(region_id);
-- idx_users_captain and idx_users_grade_cohort are created in db._migrate() —
-- captain_id and grade_cohort_id may not exist
-- yet on a database created before that column was added.

-- One-time passcodes for passwordless login. Only the hash is stored.
CREATE TABLE IF NOT EXISTS otp_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier  TEXT NOT NULL,              -- email or phone as typed
    code_hash   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed_at TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_otp_ident ON otp_codes(identifier, consumed_at);

-- --------------------------------------------------------------------------
-- Journey structure (all admin-editable)
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stages (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    key                   TEXT NOT NULL UNIQUE,
    title                 TEXT NOT NULL,
    subtitle              TEXT NOT NULL DEFAULT '',
    description           TEXT NOT NULL DEFAULT '',
    locked_hint           TEXT NOT NULL DEFAULT '',  -- shown on a greyed-out card
    sort_order            INTEGER NOT NULL DEFAULT 0,
    is_mandatory          INTEGER NOT NULL DEFAULT 1,
    -- how this stage is judged complete:
    --   'components'   -> all mandatory components complete
    --   'admin_marked' -> an admin marks it (e.g. Orientation attendance)
    completion_rule       TEXT NOT NULL DEFAULT 'components',
    -- unlock rule: this stage opens once the referenced stage is complete.
    -- NULL means it is open from day one.
    unlock_after_stage_id INTEGER REFERENCES stages(id),
    deadline_days         INTEGER,                   -- days from unlock, NULL = none
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    archived_at           TEXT,
    CHECK (completion_rule IN ('components', 'admin_marked'))
);

CREATE TABLE IF NOT EXISTS components (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id        INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    key             TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    is_mandatory    INTEGER NOT NULL DEFAULT 1,
    -- 'sub_items'    -> all mandatory sub-items complete
    -- 'self_marked'  -> tutor ticks the component itself (no sub-items)
    -- 'admin_marked' -> only an admin can complete it
    completion_rule TEXT NOT NULL DEFAULT 'sub_items',
    -- NULL region_id => shown to every region. Set => region-specific content.
    region_id       INTEGER REFERENCES regions(id),
    grade_cohort_id INTEGER REFERENCES grade_cohorts(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    CHECK (completion_rule IN ('sub_items', 'self_marked', 'admin_marked'))
);

CREATE INDEX IF NOT EXISTS idx_components_stage ON components(stage_id, sort_order);

CREATE TABLE IF NOT EXISTS sub_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id    INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    -- self-reference lets admins nest one level (e.g. Policies -> W-H Policy)
    parent_id       INTEGER REFERENCES sub_items(id) ON DELETE CASCADE,
    key             TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    instructions    TEXT NOT NULL DEFAULT '',
    -- 'task'   tutor ticks it off
    -- 'policy' must be read & acknowledged (document required)
    -- 'upload' tutor submits a file (headshot, video)
    -- 'link'   tutor visits an external link then ticks it
    -- 'group'  heading only, never completed on its own
    kind            TEXT NOT NULL DEFAULT 'task',
    accept_mime     TEXT NOT NULL DEFAULT '',   -- for 'upload', e.g. 'image/*'
    max_upload_mb   INTEGER,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    is_mandatory    INTEGER NOT NULL DEFAULT 1,
    region_id       INTEGER REFERENCES regions(id),
    grade_cohort_id INTEGER REFERENCES grade_cohorts(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    CHECK (kind IN ('task', 'policy', 'upload', 'link', 'group'))
);

CREATE INDEX IF NOT EXISTS idx_sub_items_component ON sub_items(component_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_sub_items_parent    ON sub_items(parent_id);

-- Orientation agenda (Stage 1). Ordered, fully admin-managed.
CREATE TABLE IF NOT EXISTS agenda_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id    INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agenda_stage ON agenda_items(stage_id, sort_order);

-- --------------------------------------------------------------------------
-- Links & documents
-- --------------------------------------------------------------------------

-- Any URL a tutor might click. Attach to at most one journey node; leave all
-- three NULL for a global link addressed by `key` (e.g. app download).
CREATE TABLE IF NOT EXISTS links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    url          TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    stage_id     INTEGER REFERENCES stages(id) ON DELETE CASCADE,
    component_id INTEGER REFERENCES components(id) ON DELETE CASCADE,
    sub_item_id  INTEGER REFERENCES sub_items(id) ON DELETE CASCADE,
    region_id    INTEGER REFERENCES regions(id),
    grade_cohort_id INTEGER REFERENCES grade_cohorts(id),
    sort_order   INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK ((stage_id IS NOT NULL) + (component_id IS NOT NULL)
           + (sub_item_id IS NOT NULL) <= 1)
);

CREATE INDEX IF NOT EXISTS idx_links_stage    ON links(stage_id);
CREATE INDEX IF NOT EXISTS idx_links_comp     ON links(component_id);
CREATE INDEX IF NOT EXISTS idx_links_sub_item ON links(sub_item_id);

-- A document is a logical slot ("Pause & Leave Policy"); its file lives in
-- document_versions so old copies are retained with an effective-from date.
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'policy',  -- policy|deck|video|sample|guide
    stage_id     INTEGER REFERENCES stages(id) ON DELETE SET NULL,
    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    sub_item_id  INTEGER REFERENCES sub_items(id) ON DELETE SET NULL,
    region_id    INTEGER REFERENCES regions(id),
    grade_cohort_id INTEGER REFERENCES grade_cohorts(id),
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    archived_at  TEXT,
    drive_url    TEXT,          -- Google Drive link; when set, used instead of an upload
    CHECK ((stage_id IS NOT NULL) + (component_id IS NOT NULL)
           + (sub_item_id IS NOT NULL) <= 1)
);

CREATE INDEX IF NOT EXISTS idx_documents_sub_item ON documents(sub_item_id);

CREATE TABLE IF NOT EXISTS document_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_no     INTEGER NOT NULL,
    filename       TEXT NOT NULL,
    storage_key    TEXT NOT NULL,           -- path/key in the object store
    mime_type      TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    effective_from TEXT NOT NULL,           -- tutors see the newest version <= now
    notes          TEXT NOT NULL DEFAULT '',
    uploaded_by    INTEGER REFERENCES users(id),
    created_at     TEXT NOT NULL,
    UNIQUE (document_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_docver_doc
    ON document_versions(document_id, effective_from DESC, version_no DESC);

-- --------------------------------------------------------------------------
-- Orientation logistics (defined after `documents` so the deck FK resolves)
-- --------------------------------------------------------------------------

-- Scheduled Orientation calls. Tutors are invited to one.
CREATE TABLE IF NOT EXISTS orientation_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    zoom_link        TEXT NOT NULL DEFAULT '',
    starts_at        TEXT,                  -- ISO 8601 local time
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    host_name        TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT '',
    region_id        INTEGER REFERENCES regions(id),
    deck_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orientation_invites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES orientation_sessions(id) ON DELETE CASCADE,
    invited_at TEXT NOT NULL,
    UNIQUE (user_id, session_id)
);

CREATE TABLE IF NOT EXISTS orientation_attendance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES orientation_sessions(id) ON DELETE SET NULL,
    attended   INTEGER NOT NULL DEFAULT 1,
    source     TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'csv'
    marked_by  INTEGER REFERENCES users(id),
    marked_at  TEXT NOT NULL,
    UNIQUE (user_id)
);

-- --------------------------------------------------------------------------
-- Class-with-a-student scheduling
-- --------------------------------------------------------------------------

-- Admin feeds available slots (a real student + time); a tutor picks one.
CREATE TABLE IF NOT EXISTS class_slots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    starts_at        TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    student_name     TEXT NOT NULL DEFAULT '',
    grade_subject    TEXT NOT NULL DEFAULT '',
    region_id        INTEGER REFERENCES regions(id),
    grade_cohort_id  INTEGER REFERENCES grade_cohorts(id),
    notes            TEXT NOT NULL DEFAULT '',
    tutor_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- 'open' -> any matching tutor may book it; 'booked' -> tutor_id owns it
    status           TEXT NOT NULL DEFAULT 'open',
    booked_at        TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CHECK (status IN ('open', 'booked', 'completed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_class_slots_tutor  ON class_slots(tutor_id);
CREATE INDEX IF NOT EXISTS idx_class_slots_status ON class_slots(status, starts_at);

-- --------------------------------------------------------------------------
-- CFU quiz (policy comprehension check)
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS quiz_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_item_id INTEGER NOT NULL REFERENCES sub_items(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_quiz_questions_item
    ON quiz_questions(sub_item_id, sort_order);

CREATE TABLE IF NOT EXISTS quiz_choices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    is_correct  INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_quiz_choices_question
    ON quiz_choices(question_id, sort_order);

-- One row per submission; reattempts are permitted, so a policy step is only
-- marked complete once an attempt gets every question right.
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sub_item_id  INTEGER NOT NULL REFERENCES sub_items(id) ON DELETE CASCADE,
    score        INTEGER NOT NULL,
    total        INTEGER NOT NULL,
    passed       INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quiz_attempts_user ON quiz_attempts(user_id, sub_item_id);

-- --------------------------------------------------------------------------
-- Tutor progress
-- --------------------------------------------------------------------------

-- One row per (tutor, journey node). Polymorphic so the same engine tracks
-- stages, components and sub-items.
CREATE TABLE IF NOT EXISTS tutor_progress (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type     TEXT NOT NULL,          -- 'stage' | 'component' | 'sub_item'
    target_id       INTEGER NOT NULL,
    -- 'in_progress' | 'submitted' (awaiting admin) | 'completed' | 'rejected'
    status          TEXT NOT NULL DEFAULT 'in_progress',
    started_at      TEXT,
    submitted_at    TEXT,
    completed_at    TEXT,
    rejected_reason TEXT NOT NULL DEFAULT '',
    reviewed_by     INTEGER REFERENCES users(id),
    notes           TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL,
    UNIQUE (user_id, target_type, target_id),
    CHECK (target_type IN ('stage', 'component', 'sub_item')),
    CHECK (status IN ('in_progress', 'submitted', 'completed', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_progress_user ON tutor_progress(user_id, target_type);

-- Timestamped proof that a tutor read a specific *version* of a policy.
CREATE TABLE IF NOT EXISTS policy_acknowledgements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sub_item_id         INTEGER NOT NULL REFERENCES sub_items(id) ON DELETE CASCADE,
    document_version_id INTEGER REFERENCES document_versions(id),
    acknowledged_at     TEXT NOT NULL,
    ip_address          TEXT NOT NULL DEFAULT '',
    UNIQUE (user_id, sub_item_id, document_version_id)
);

CREATE INDEX IF NOT EXISTS idx_ack_user ON policy_acknowledgements(user_id);

-- A tutor watching a video document to completion. Task/link steps whose
-- video hasn't been watched yet can't be marked done (see toggle_sub_item).
CREATE TABLE IF NOT EXISTS video_views (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    watched_at  TEXT NOT NULL,
    UNIQUE (user_id, document_id)
);

CREATE INDEX IF NOT EXISTS idx_video_views_user ON video_views(user_id);

-- Files a tutor submits (headshot, 1-minute video, anything else admins add).
CREATE TABLE IF NOT EXISTS submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sub_item_id  INTEGER NOT NULL REFERENCES sub_items(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    storage_key  TEXT NOT NULL,
    mime_type    TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'submitted',  -- submitted|approved|rejected
    review_notes TEXT NOT NULL DEFAULT '',
    reviewed_by  INTEGER REFERENCES users(id),
    submitted_at TEXT NOT NULL,
    superseded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id, sub_item_id);

-- --------------------------------------------------------------------------
-- Class review & coach compliance (owned by a tutor's Captain, post-enrollment)
-- --------------------------------------------------------------------------

-- One row per (tutor, class number 1-8). A Captain logs this once the class
-- has happened, so it doubles as "has this class been reviewed yet".
CREATE TABLE IF NOT EXISTS class_reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    class_number      INTEGER NOT NULL,
    -- 'reviewed'  -> logged, no concerns
    -- 'flagged'   -> logged, red flag raised
    status            TEXT NOT NULL DEFAULT 'reviewed',
    feedback_note     TEXT NOT NULL DEFAULT '',
    red_flag_reason   TEXT NOT NULL DEFAULT '',
    -- set on the class 5 review (Progress Report) and class 8 review (PTM)
    milestone         TEXT NOT NULL DEFAULT '',
    reviewed_by       INTEGER REFERENCES users(id),
    reviewed_at       TEXT NOT NULL,
    UNIQUE (user_id, class_number),
    CHECK (class_number BETWEEN 1 AND 8),
    CHECK (status IN ('reviewed', 'flagged')),
    CHECK (milestone IN ('', 'progress_report', 'ptm'))
);

CREATE INDEX IF NOT EXISTS idx_class_reviews_user ON class_reviews(user_id);

-- Individual compliance incidents. A tutor's Compliance Rating is computed
-- on read from these rows (see progress.compliance_state) rather than stored,
-- so the scoring model can change without a backfill.
CREATE TABLE IF NOT EXISTS compliance_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,
    notes        TEXT NOT NULL DEFAULT '',
    logged_by    INTEGER REFERENCES users(id),
    occurred_at  TEXT NOT NULL,
    CHECK (event_type IN ('class_late_login', 'class_no_show', 'trial_late_login',
                          'trial_no_show', 'trial_ack_late'))
);

CREATE INDEX IF NOT EXISTS idx_compliance_events_user ON compliance_events(user_id, occurred_at DESC);

-- --------------------------------------------------------------------------
-- Notifications, audit, settings
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'info',  -- info|unlock|deadline|approval
    created_at TEXT NOT NULL,
    read_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, read_at);

-- Email is queued here rather than sent directly; point a worker or SMTP relay
-- at this table in production (see README).
CREATE TABLE IF NOT EXISTS email_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    to_address TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    error      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id  INTEGER REFERENCES users(id),
    actor_label    TEXT NOT NULL DEFAULT '',   -- kept even if the user is removed
    action         TEXT NOT NULL,              -- e.g. 'stage.update'
    entity_type    TEXT NOT NULL DEFAULT '',
    entity_id      INTEGER,
    summary        TEXT NOT NULL DEFAULT '',
    before_json    TEXT NOT NULL DEFAULT '',
    after_json     TEXT NOT NULL DEFAULT '',
    ip_address     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_entity  ON audit_log(entity_type, entity_id);

-- Free-form editable copy (welcome banner, certificate wording, stalled-days
-- threshold, ...). Keeps one-off strings out of the codebase.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    updated_at  TEXT
);
