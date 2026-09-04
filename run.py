#!/usr/bin/env python3
"""Cuemath Coach Onboarding & Training Tool — entry point.

    python3 run.py init                 create the database
    python3 run.py seed [--demo]        load the onboarding journey (Part 1 content)
    python3 run.py serve [--port 8000]  start the web server
    python3 run.py create-admin         add an admin or viewer account
    python3 run.py sweep                queue deadline reminder notifications
    python3 run.py outbox               show queued emails (dev helper)
    python3 run.py reset --yes          delete the database and uploads

`serve` runs init+seed automatically the first time, so a bare
`python3 run.py` is enough to get going.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config, db, micro, seed as seed_module  # noqa: E402


def _flag(args, name, default=None):
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return default


def cmd_init(args):
    path = db.init_db()
    print("Database ready at %s" % path)


def cmd_seed(args):
    seed_module.seed(demo="--demo" in args)
    print("Seed complete.")


def cmd_serve(args):
    # Postgres always starts from schema_postgres.sql (no local file to check),
    # and ensure_admin() is idempotent, so just skip this SQLite-only shortcut.
    first_run = not db.USE_POSTGRES and not os.path.exists(config.DB_PATH)
    db.init_db()
    if not db.scalar("SELECT COUNT(*) FROM stages", (), 0):
        print("No content found — seeding the onboarding journey...")
        seed_module.seed(demo="--demo" in args)
    else:
        if os.environ.get("CUEMATH_SEED_ON_BOOT") == "1":
            # How a content change reaches a hosted database, where there's no
            # shell to run `run.py seed` from. Set the variable, let it
            # redeploy, then turn it off again — left on, every restart would
            # overwrite copy that was edited in the admin screens.
            print("CUEMATH_SEED_ON_BOOT=1 — refreshing content from seed...")
            seed_module.seed()
        if first_run:
            seed_module.ensure_admin()

    from app.webapp import bootstrap_check, create_app
    warning = bootstrap_check()
    if warning:
        print("  ! %s" % warning)
    app = create_app()
    host = _flag(args, "--host", config.HOST)
    port = int(_flag(args, "--port", config.PORT))
    print("  Tutors: http://%s:%d/login    Admin: http://%s:%d/admin/login"
          % (host, port, host, port))
    micro.serve(app, host=host, port=port)


def cmd_create_admin(args):
    from app import security
    email = _flag(args, "--email") or input("Email: ").strip()
    password = _flag(args, "--password") or input("Password: ").strip()
    name = _flag(args, "--name") or input("Name: ").strip() or email
    role = _flag(args, "--role", "admin")
    db.init_db()
    if not db.one("SELECT 1 FROM roles WHERE key = ? AND can_admin = 1", (role,)):
        sys.exit("Unknown admin role %r. Use 'admin' or 'viewer'." % role)
    problem = security.password_problem(password)
    if problem:
        sys.exit(problem)
    email = security.normalise_email(email)
    if db.one("SELECT 1 FROM users WHERE email = ?", (email,)):
        sys.exit("A user with that email already exists.")
    db.insert("users", {
        "name": name, "email": email, "phone": None,
        "password_hash": security.hash_password(password),
        "role_key": role, "region_id": None, "created_at": db.now(),
    })
    print("Created %s (%s). Sign in at /admin/login" % (email, role))


def cmd_sweep(args):
    from app import notify
    db.init_db()
    print("Queued %d deadline reminder(s)." % notify.deadline_sweep())


def cmd_outbox(args):
    from app import notify
    db.init_db()
    rows = notify.pending_emails(50)
    if not rows:
        print("Outbox is empty.")
        return
    for row in rows:
        print("-" * 68)
        print("To:      %s" % row["to_address"])
        print("Subject: %s" % row["subject"])
        print(row["body"])
    print("-" * 68)
    print("%d queued email(s)." % len(rows))


def cmd_reset(args):
    if db.USE_POSTGRES:
        sys.exit("reset only deletes the local SQLite file — it does nothing to "
                 "the Postgres database DATABASE_URL points at, so it's disabled "
                 "here to avoid a misleading 'Reset done'. Drop/recreate the "
                 "Postgres database directly if you really want to wipe it.")
    if "--yes" not in args:
        sys.exit("This deletes the database and every upload. Re-run with --yes.")
    for path in (config.DB_PATH, config.DB_PATH + "-wal", config.DB_PATH + "-shm"):
        if os.path.exists(path):
            os.remove(path)
    if os.path.isdir(config.UPLOAD_DIR):
        shutil.rmtree(config.UPLOAD_DIR)
    config.ensure_dirs()
    print("Reset done. Run: python3 run.py seed")


COMMANDS = {
    "init": cmd_init,
    "seed": cmd_seed,
    "serve": cmd_serve,
    "create-admin": cmd_create_admin,
    "sweep": cmd_sweep,
    "outbox": cmd_outbox,
    "reset": cmd_reset,
}


def main(argv):
    args = argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command = args[0] if args and not args[0].startswith("-") else "serve"
    handler = COMMANDS.get(command)
    if handler is None:
        print(__doc__)
        return 1
    handler(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
