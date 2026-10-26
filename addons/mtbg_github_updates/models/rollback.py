"""Going back (owner, 2026-10-26: "functionality should include rollback").

* **Roll back an update** (a batch or a package): the code AND the database (and the files) as they were before
  it. The batch's backup is restored into a new database, which takes the site's name (the current one is kept a
  few days as <db>_before_rollback_<time>); the filestore snapshot taken with the backup comes back; the code links
  go back to the versions that ran before. Every update made after that one is undone too (the database goes back
  past them). Data entered since the backup is lost: the preview says how much, per kind of record.
* **Go back (code only)**, one module: its previous version, the database upgraded with it, the data kept. Offered
  only when it is safe: no migration script in the versions undone, no installed module needing the newer version.

Limits (the page says them): a code-only go-back cannot undo a data migration; the full rollback loses what was
entered after the backup, and does not undo what was sent to other services since (emails, Zoho, Stripe); other
databases on this server share the code.

Backups are kept for the last 10 batches or 30 days, whichever keeps more, and always the newest one before each
module's last update; a database kept by a rollback is dropped after 7 days.
"""
import json
import logging
import os
import shutil
import threading
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import maintenance, reexec, store
from .update import manifest_requires

_logger = logging.getLogger(__name__)
KEEP_BATCHES = 10
KEEP_DAYS = 30
KEEP_ROLLED_DB_DAYS = 7
# tables whose new rows are not "data entered" (logs, caches, sessions, the updater's own)
QUIET_PREFIXES = ("ir_", "bus_", "base_", "res_users_log", "res_users_settings", "mail_tracking", "mtbg_github_",
                  "auth_", "web_tour", "website_visitor", "website_track", "mail_notification", "mail_mail",
                  "iap_", "digest_", "mtbg_update_", "cfd_update_")


class MtbgGithubBatch(models.Model):
    _inherit = "mtbg.github.batch"

    # ------------------------------------------------------------------ what a rollback would undo
    def _mtbg_rollback_scope(self):
        """This batch and every batch after it (not undone, not take-overs): their modules, each going back to the
        version it ran before the first of them."""
        self.ensure_one()
        later = self.search([("id", ">=", self.id), ("undone", "=", False),
                             ("kind", "in", ("update", "install", "package", "revert", "rollback"))], order="id")
        lines = self.env["mtbg.github.update"].search([("batch_id", "in", later.ids)], order="id")
        back = {}
        for line in lines:
            back.setdefault(line.module, line)
        return later, back

    def _mtbg_rollback_problem(self):
        self.ensure_one()
        if self.kind not in ("update", "install", "package", "revert") or self.undone:
            return _("This update cannot be rolled back.")
        if not self.backup_path or self.backup_purged or not os.path.isfile(self.backup_path):
            return _("Its backup is no longer kept.")
        if self.search_count([("id", ">", self.id), ("kind", "=", "restore"), ("restore_state", "=", "done")]):
            return _("A rollback was made after it: roll back a later update instead.")
        if self.line_ids.filtered(lambda l: l.state == "restarting"):
            return _("It is still running.")
        return ""

    @api.model
    def _mtbg_data_since(self, when):
        """[(what, rows created or changed since ``when``)] of the tables with a write_date, the busiest first.
        Rows last written by the superuser (the upgrade itself, module data) are not people's data: left out."""
        cr = self.env.cr
        cr.execute("""SELECT c.table_name, EXISTS (SELECT 1 FROM information_schema.columns u
                              WHERE u.table_name = c.table_name AND u.table_schema = 'public'
                                AND u.column_name = 'write_uid')
                         FROM information_schema.columns c
                       JOIN information_schema.tables t ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                      WHERE c.column_name = 'write_date' AND c.table_schema = 'public' AND t.table_type = 'BASE TABLE'""")
        tables = [(row[0], row[1]) for row in cr.fetchall() if not row[0].startswith(QUIET_PREFIXES)]
        counts = []
        for table, has_uid in tables:
            people = " AND write_uid IS DISTINCT FROM 1" if has_uid else ""
            cr.execute('SELECT count(*) FROM "%s" WHERE write_date > %%s%s' % (table.replace('"', ""), people),  # noqa: S608
                       (when,))
            count = cr.fetchone()[0]
            if count:
                counts.append((table, count))
        models_by_table = {m.model.replace(".", "_"): m.name for m in self.env["ir.model"].sudo().search([])}
        return [(models_by_table.get(table, table), count) for table, count in sorted(counts, key=lambda c: -c[1])]

    def mtbg_rollback_preview(self):
        """What "Roll back" would do: the updates undone, each module's version back, the backup's time, the data
        entered since, and what stops it."""
        self.env["mtbg.github.update"]._mtbg_check_admin()
        self.ensure_one()
        later, back = self._mtbg_rollback_scope()
        problems = [p for p in [self._mtbg_rollback_problem()] if p]
        dbname = self.env.cr.dbname
        try:
            ok, problem = store.database_rights(dbname)
        except Exception as error:  # noqa: BLE001
            ok, problem = False, str(error)
        if not ok:
            problems.append(_("The database cannot be restored here: %s.", problem))
        restart = reexec.restart_problem(self.env)
        if restart:
            problems.append(restart)
        busy = self.env["mtbg.github.update"]._mtbg_busy_elsewhere()
        if busy:
            problems.append(busy)
        if self.backup_path and os.path.isfile(self.backup_path):
            free = store.free_bytes()
            if free is not None and free < os.path.getsize(self.backup_path) * 4:
                problems.append(_("Not enough free disk space for the restore."))
        return {
            "batch": self.name, "title": self.title or "", "database": dbname,
            "backup_at": fields.Datetime.to_string(self.backup_at) if self.backup_at else False,
            "files": bool(self.snapshot_path and os.path.isdir(self.snapshot_path)),
            "updates": [{"name": b.name, "title": b.title or dict(b._fields["kind"]._description_selection(self.env))[b.kind],
                         "date": fields.Datetime.to_string(b.create_date)} for b in later],
            "modules": [{"module": module, "now": store.active_version(module) or line.to_version or "",
                         "back_to": line.from_version or "", "removed": line.kind == "install"}
                        for module, line in sorted(back.items())],
            "data": [{"what": what, "count": count} for what, count in
                     (self._mtbg_data_since(self.backup_at) if self.backup_at else [])[:15]],
            "problems": problems,
        }

    # ------------------------------------------------------------------ the rollback (code + database + files)
    def mtbg_rollback(self, confirm_database):
        """Roll back this update and every later one: restore the database and files of its backup, the code it
        ran, then restart. ``confirm_database``: the database's name, typed by the administrator."""
        Update = self.env["mtbg.github.update"]
        Update._mtbg_check_admin()
        self.ensure_one()
        dbname = self.env.cr.dbname
        if (confirm_database or "").strip() != dbname:
            raise UserError(_("Type the database's name (%s) to confirm.", dbname))
        preview = self.mtbg_rollback_preview()
        if preview["problems"]:
            raise UserError("\n".join(preview["problems"]))
        later, back = self._mtbg_rollback_scope()
        stamp = time.strftime("%Y%m%d%H%M%S")
        restore = self.create({
            "name": "rb-" + stamp, "kind": "restore", "restore_state": "restoring",
            "title": _("Rollback of %s", self.title or self.name), "backup_at": self.backup_at,
            "kept_database": "%s_before_rollback_%s" % (dbname[:30], stamp)})
        plan = {
            "restore": restore.name, "of": self.name, "database": dbname, "dump": self.backup_path,
            "snapshot": self.snapshot_path or "", "restored": "%s_restore_%s" % (dbname[:40], stamp),
            "keep_as": restore.kept_database, "user": self.env.user.login,
            "links": {module: line.previous_target or None for module, line in back.items()},
            "undone": later.mapped("name"),
            "history": self._mtbg_history_rows(later | restore),
        }
        journal = self._mtbg_journal(dbname, restore.name, dict(plan, state="started"))
        Update._mtbg_commit()
        registry = self.env.registry
        if self.env.context.get("mtbg_no_restart"):
            return {"journal": journal, "plan": plan}
        threading.Thread(target=self._mtbg_restore_work, args=(registry, plan, journal), daemon=True).start()
        return {"type": "ir.actions.client", "tag": "mtbg_github_progress",
                "params": {"restore": restore.name}, "name": _("Rolling back")}

    @api.model
    def _mtbg_history_rows(self, batches):
        """The history the restored database does not have (made after its backup): written into it afterwards."""
        rows = []
        for batch in batches:
            rows.append({"batch": {"name": batch.name, "kind": batch.kind, "title": batch.title or "",
                                   "backup_path": batch.backup_path or "", "snapshot_path": batch.snapshot_path or "",
                                   "backup_at": fields.Datetime.to_string(batch.backup_at) if batch.backup_at else None,
                                   "message": batch.message or "", "kept_database": batch.kept_database or "",
                                   "created": fields.Datetime.to_string(batch.create_date)},
                         "lines": [{"module": l.module, "kind": l.kind, "from_version": l.from_version or "",
                                    "to_version": l.to_version or "", "state": l.state, "message": l.message or "",
                                    "batch": l.batch, "previous_target": l.previous_target or "",
                                    "target": l.target or ""} for l in batch.line_ids]})
        return rows

    @api.model
    def _mtbg_journal(self, dbname, name, data):
        """The rollback's journal, a file outside the database it replaces."""
        folder = store.path("runs", store.check_db(dbname))
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, "%s.json" % name)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, default=str)
        return target

    @api.model
    def _mtbg_restore_work(self, registry, plan, journal):
        """In a thread, after the answer reached the browser: maintenance, the backup into a new database, the swap,
        the files, the code, the history, the restart. Before the swap, any failure leaves everything as it was."""
        dbname = plan["database"]
        time.sleep(1.5)

        def note(state, message=""):
            plan.update(state=state, message=message, at=time.strftime("%Y-%m-%d %H:%M:%S"))
            with open(journal, "w", encoding="utf-8") as handle:
                json.dump(plan, handle, indent=1, default=str)
            _logger.info("Updates: rollback %s: %s %s", plan["restore"], state, message)

        swapped = False
        try:
            maintenance.start(dbname, plan["restore"])
            maintenance.wait_for_jobs(dbname)
            note("restoring")
            store.restore_database(plan["dump"], plan["restored"])
            self._mtbg_write_history(plan)
            note("swapping")
            store.swap_databases(dbname, plan["restored"], plan["keep_as"])
            swapped = True
        except Exception as error:  # noqa: BLE001 - before the swap: the site stays as it was
            _logger.exception("Updates: rollback %s failed before the swap", plan["restore"])
            try:
                store.drop_database(plan["restored"])
            except Exception:  # noqa: BLE001
                _logger.exception("Updates: %s not dropped", plan["restored"])
            note("failed", str(error))
            try:
                with registry.cursor() as cr:
                    cr.execute("UPDATE mtbg_github_batch SET restore_state = 'failed', message = %s WHERE name = %s",
                               (str(error)[-2000:], plan["restore"]))
            except Exception:  # noqa: BLE001
                _logger.exception("Updates: rollback %s: its failure not written", plan["restore"])
            maintenance.stop(dbname)
            return
        problems = []
        try:
            store.swap_filestore(dbname, plan["snapshot"], plan["keep_as"])
        except Exception as error:  # noqa: BLE001 - the database is back; the files are a lesser loss
            problems.append(str(error))
        for module, target in plan["links"].items():
            try:
                store.switch(module, target)
            except Exception as error:  # noqa: BLE001
                problems.append("%s: %s" % (module, error))
        note("done" if not problems else "done_with_problems", "\n".join(problems))
        if problems:
            try:
                from odoo import sql_db
                with sql_db.db_connect(dbname).cursor() as cr:
                    cr.execute("UPDATE mtbg_github_batch SET message = %s WHERE name = %s",
                               ("\n".join(problems)[-2000:], plan["restore"]))
            except Exception:  # noqa: BLE001
                _logger.exception("Updates: rollback %s: its problems not written", plan["restore"])
        if swapped:
            from odoo.service import server
            server.restart()

    @api.model
    def _mtbg_write_history(self, plan):
        """Into the restored database (before it takes the site's name): the updates made after its backup, marked
        undone, and this rollback, done."""
        from odoo import sql_db
        with sql_db.db_connect(plan["restored"]).cursor() as cr:
            cr.execute("SELECT to_regclass('mtbg_github_batch')")
            if not cr.fetchone()[0]:
                return  # the backup is older than this updater
            for row in plan["history"]:
                batch = row["batch"]
                is_restore = batch["name"] == plan["restore"]
                cr.execute("""INSERT INTO mtbg_github_batch (name, kind, title, backup_path, snapshot_path, backup_at,
                                    message, undone, restore_state, kept_database, create_date, write_date)
                              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() at time zone 'UTC')
                              ON CONFLICT (name) DO UPDATE SET undone = EXCLUDED.undone
                              RETURNING id""",
                           (batch["name"], batch["kind"], batch["title"], batch["backup_path"], batch["snapshot_path"],
                            batch["backup_at"], batch["message"], not is_restore,
                            "done" if is_restore else None, batch["kept_database"] or None, batch["created"]))
                batch_id = cr.fetchone()[0]
                for line in row["lines"]:
                    cr.execute("""INSERT INTO mtbg_github_update (batch, batch_id, module, kind, from_version,
                                        to_version, state, message, previous_target, target, create_date, write_date)
                                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() at time zone 'UTC',
                                          now() at time zone 'UTC')""",
                               (line["batch"], batch_id, line["module"], line["kind"], line["from_version"],
                                line["to_version"], "rolled_back" if line["state"] == "applied" else line["state"],
                                line["message"], line["previous_target"], line["target"]))
            cr.commit()

    @api.model
    def mtbg_restore_progress(self, name):
        """For the progress screen: the rollback's state, from its journal (the database may be the restored one)."""
        self.env["mtbg.github.update"]._mtbg_check_admin()
        if not name or not name.startswith("rb-") or not name[3:].isdigit():
            return {"state": "unknown"}
        try:
            with open(os.path.join(store.path("runs", store.check_db(self.env.cr.dbname)), name + ".json"),
                      encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"state": "unknown"}
        return {"state": data.get("state"), "message": data.get("message", ""),
                "modules": [{"module": m, "back_to": os.path.basename(os.path.dirname(t)) if t else "—"}
                            for m, t in sorted((data.get("links") or {}).items())]}

    # ------------------------------------------------------------------ retention
    @api.model
    def _cron_retention(self):
        """Backups: the last KEEP_BATCHES batches or KEEP_DAYS days, whichever keeps more, and the newest one before
        each module's last update; a database kept by a rollback: KEEP_ROLLED_DB_DAYS days."""
        batches = self.sudo().search([("backup_path", "!=", False), ("backup_purged", "=", False)], order="id desc")
        keep = batches[:KEEP_BATCHES]
        limit = fields.Datetime.now() - timedelta(days=KEEP_DAYS)
        keep |= batches.filtered(lambda b: b.create_date and b.create_date >= limit)
        newest_per_module = {}
        for batch in batches:
            for module in batch.line_ids.mapped("module"):
                newest_per_module.setdefault(module, batch)
        keep |= self.browse([b.id for b in newest_per_module.values()])
        for batch in batches - keep:
            for target in (batch.backup_path, batch.snapshot_path):
                if not target:
                    continue
                real = os.path.realpath(target)
                if not real.startswith(os.path.realpath(store.path("backups")) + os.sep):
                    continue
                if os.path.isdir(real):
                    shutil.rmtree(real, ignore_errors=True)
                elif os.path.isfile(real):
                    os.remove(real)
            batch.backup_purged = True
        old = fields.Datetime.now() - timedelta(days=KEEP_ROLLED_DB_DAYS)
        for restore in self.sudo().search([("kind", "=", "restore"), ("kept_database", "!=", False),
                                           ("create_date", "<", old)]):
            try:
                store.drop_database(restore.kept_database)
                shutil.rmtree(store.filestore_dir(restore.kept_database), ignore_errors=True)
                restore.kept_database = False
            except Exception as error:  # noqa: BLE001
                _logger.warning("Updates: %s not dropped: %s", restore.kept_database, error)


class MtbgGithubUpdate(models.Model):
    _inherit = "mtbg.github.update"

    @api.model
    def _mtbg_check_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only administrators can manage updates."))

    # ------------------------------------------------------------------ go back, code only (one module)
    @api.model
    def _mtbg_revert_line(self, module):
        """The module's last applied update (not undone), the one going back would reverse."""
        return self.search([("module", "=", module), ("state", "=", "applied"),
                            ("kind", "in", ("update", "revert", "install")), ("batch_id.undone", "=", False)],
                           order="id desc", limit=1)

    @api.model
    def mtbg_revert_preview(self, module):
        self._mtbg_check_admin()
        line = self._mtbg_revert_line(module)
        if not line or line.kind != "update":
            return {"ok": False, "reason": _("%s was not updated from here: there is nothing to go back to.", module)}
        reasons = []
        current = store.active_target(module)
        if store.has_migrations(current, line.from_version, line.to_version):
            reasons.append(_("The newer version changed data when it was installed (a migration): only the full "
                             "rollback, with the database, can undo it."))
        later = self.search([("module", "=", module), ("id", ">", line.id), ("state", "=", "applied")], limit=1)
        if later:
            reasons.append(_("A later update touched it."))
        if not line.previous_target:
            reasons.append(_("The version before was not loaded from a store: use the full rollback."))
        for name, manifest in self.env["mtbg.github.source"]._mtbg_installed_manifests().items():
            least = manifest_requires(manifest).get(module)
            if least and store.version_key(line.from_version) < store.version_key(least):
                reasons.append(_("%(other)s needs %(module)s %(least)s or later.", other=name, module=module, least=least))
        return {"ok": not reasons, "reasons": reasons, "module": module, "from": line.to_version,
                "to": line.from_version, "batch": line.batch}

    @api.model
    def mtbg_revert(self, module):
        """Go back one module to the version it ran before its last update; the database is upgraded with that code
        and keeps its data."""
        preview = self.mtbg_revert_preview(module)
        if not preview["ok"]:
            raise UserError("\n".join(preview.get("reasons") or [preview.get("reason", "")]))
        line = self._mtbg_revert_line(module)
        release = self.env["mtbg.github.release"].search(
            [("module", "=", module), ("version", "=", line.from_version)], limit=1)
        if release:
            return self._mtbg_start([(module, release)], kind="revert", title=_("Back to %s", line.from_version))
        return self._mtbg_start_to_folder(module, line.previous_target, line.from_version)

    @api.model
    def _mtbg_start_to_folder(self, module, folder, version):
        """Going back to a stored version that has no release listed (an adopted one): the same batch, its folder."""
        self._mtbg_check_ready()
        name = "%s" % os.urandom(6).hex()
        backup, snapshot = store.backup_database(self.env.cr.dbname, name)
        header = self.env["mtbg.github.batch"].create({"name": name, "kind": "revert", "backup_path": backup,
                                                        "snapshot_path": snapshot, "backup_at": fields.Datetime.now(),
                                                        "title": _("Back to %s", version)})
        before = store.active_target(module)
        store.switch(module, folder)
        log = self.create({"batch": name, "batch_id": header.id, "module": module, "kind": "revert",
                           "from_version": self._mtbg_version_of(before, module), "to_version": version,
                           "previous_target": before or False, "target": folder, "switched_at": time.time()})
        self._mtbg_commit()
        log._mtbg_upgrade_then_restart()
        return {"type": "ir.actions.client", "tag": "mtbg_github_progress", "params": {"batch": name},
                "name": _("Updating")}
