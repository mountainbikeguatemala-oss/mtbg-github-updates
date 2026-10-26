"""Maintenance while an update upgrades the database: a busy server (workers, scheduled jobs writing the same tables)
holds locks the upgrade needs, and its ALTER TABLE then fails on the lock timeout.

While a database is in maintenance (a flag file in the update folder, seen by every worker of the server):
* scheduled jobs are not started (the jobs running finish first: the update waits for them, with a time limit);
* web pages answer 503 "Updating, back in a minute" (the update's own progress calls and static files go through);
* right before the upgrade, the database's other sessions (this server's role) are ended, so none holds a lock.

The flag goes when the restarted server loads (``clear_after_restart``), or at once when nothing restarts; a flag
older than MAX_AGE is ignored (a crashed update never leaves a site in maintenance).
"""
import logging
import os
import re
import time

from odoo import sql_db

from . import store

_logger = logging.getLogger(__name__)

MAX_AGE = 2 * 3600
JOBS_WAIT = 180  # seconds the update waits for running scheduled jobs before ending their sessions
DB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")


def _flag(dbname):
    if not DB_RE.match(dbname or ""):
        raise store.StoreError("Unexpected database name.")
    return os.path.join(store.root(), "maintenance-%s" % dbname)


def start(dbname, batch=""):
    store.ensure_dirs()
    with open(_flag(dbname), "w") as handle:
        handle.write("%s %s\n" % (int(time.time()), batch))
    _logger.info("Updates: %s in maintenance (update %s)", dbname, batch)


def stop(dbname):
    try:
        os.remove(_flag(dbname))
    except FileNotFoundError:
        return
    _logger.info("Updates: %s out of maintenance", dbname)


def active(dbname):
    """Whether ``dbname`` is in maintenance now (cheap: one stat, for every request and every cron poll)."""
    try:
        age = time.time() - os.path.getmtime(_flag(dbname))
    except (OSError, store.StoreError):
        return False
    return age < MAX_AGE


def clear_after_restart(dbname):
    """The restarted server: a flag set before this process started belongs to the update that restarted it."""
    try:
        if os.path.getmtime(_flag(dbname)) < store.PROCESS_STARTED:
            stop(dbname)
    except (OSError, store.StoreError):
        pass


def wait_for_jobs(dbname, timeout=JOBS_WAIT):
    """Wait until no scheduled job runs on ``dbname`` (a running job keeps its ir_cron row locked). Returns True
    when they all finished, False when the time ran out (their sessions are then ended with the others)."""
    end = time.time() + timeout
    while True:
        with sql_db.db_connect(dbname).cursor() as cr:
            try:
                cr.execute("SET LOCAL lock_timeout = '2s'")
                cr.execute("SELECT id FROM ir_cron FOR UPDATE NOWAIT")
                return True
            except Exception:  # noqa: BLE001 - a lock error: a job is running
                cr.rollback()
        if time.time() > end:
            _logger.warning("Updates: scheduled jobs still running on %s after %ss: ending them", dbname, timeout)
            return False
        time.sleep(3)


def end_sessions(dbname):
    """End the other sessions of ``dbname`` (this server's role), so none holds a lock the upgrade needs. This
    process's own pooled connections are among them: the pool is emptied (the server restarts after the update)."""
    with sql_db.db_connect(dbname).cursor() as cr:
        cr.execute("""
            SELECT count(pg_terminate_backend(pid))
              FROM pg_stat_activity
             WHERE datname = current_database() AND pid <> pg_backend_pid() AND usename = current_user
               AND backend_type = 'client backend'
        """)
        ended = cr.fetchone()[0]
    sql_db.close_db(dbname)
    _logger.info("Updates: %s other session(s) of %s ended before the upgrade", ended, dbname)
    return ended


# Scheduled jobs: the server's cron runner calls IrCron._process_jobs(db) for each database; in maintenance it skips
# that database (a worker loads this module with its first registry, long before an update needs it).
from odoo.addons.base.models import ir_cron as _ir_cron  # noqa: E402

_process_jobs = _ir_cron.IrCron._process_jobs


def _process_jobs_unless_maintenance(db_name):
    if active(db_name):
        _logger.info("Updates: %s is updating, its scheduled jobs wait", db_name)
        return None
    return _process_jobs(db_name)


# (the Tours or CFD updater may have wrapped it already, for their own flag: this wraps theirs, each its own marker)
if not getattr(_ir_cron.IrCron._process_jobs, "_mtbg_github_patched", False):
    _process_jobs_unless_maintenance._mtbg_github_patched = True
    _ir_cron.IrCron._process_jobs = staticmethod(_process_jobs_unless_maintenance)
