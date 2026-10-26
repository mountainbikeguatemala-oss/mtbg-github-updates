"""The update engine and its history (the Tours and CFD updaters' engine, one for every product).

A batch is a set of extensions, each going to a chosen version (one module, several, a package):

1. a database backup and a filestore snapshot (kept for the rollback);
2. each version is put in the store (once, from its release tag) and its active link is switched to it;
3. a process of its own upgrades the database with the new code (``-u ... --stop-after-init``) while the server
   still runs the old code (a release that adds a column every request reads would otherwise break every request of
   the new code before an upgrade inside a request could add it);
4. the server restarts, so the new code is the code that runs; the progress screen (or the scheduled job, nobody
   watching) then marks the batch done. A module the process did not upgrade is upgraded there, the normal Odoo way;
5. if the upgrade fails, the links go back to what they were, the server restarts again, and the previous versions
   are upgraded back (the automatic rollback). Each step is written in the history.

``mtbg.github.batch`` is the batch (its kind, backup, package); ``mtbg.github.update`` its lines, one per extension.
"""
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import maintenance, reexec, store

_logger = logging.getLogger(__name__)
REQUIRES_KEYS = ("mtbg_requires", "mtbg_tours_requires")  # {module: minimum version} in a manifest
SPLIT_KEY = "mtbg_tours_split"


def manifest_requires(manifest):
    minimum = {}
    for key in REQUIRES_KEYS:
        for dep, version in (manifest.get(key) or {}).items():
            if version and store.version_key(version) > store.version_key(minimum.get(dep)):
                minimum[dep] = version
    return minimum


class MtbgGithubBatch(models.Model):
    _name = "mtbg.github.batch"
    _description = "Update batch"
    _order = "create_date desc, id desc"  # (a rollback writes the later history back into the restored database)

    name = fields.Char(required=True, readonly=True, index=True)
    kind = fields.Selection([
        ("update", "Update"), ("install", "Install"), ("package", "Package"), ("revert", "Go back (code only)"),
        ("rollback", "Automatic rollback"), ("restore", "Rollback (code and database)"), ("adopt", "Take over"),
        ("imported", "Earlier updater")], required=True, readonly=True)
    package_id = fields.Many2one("mtbg.github.package", readonly=True, ondelete="set null")
    title = fields.Char(readonly=True)
    backup_path = fields.Char(string="Backup", readonly=True)
    snapshot_path = fields.Char(string="Files Snapshot", readonly=True)
    backup_at = fields.Datetime(readonly=True, help="When the backup was taken: data entered after it is lost by a "
                                                    "rollback.")
    backup_purged = fields.Boolean(readonly=True)
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    message = fields.Text(readonly=True)
    undone = fields.Boolean(readonly=True, help="Undone by a rollback (code and database).")
    restore_state = fields.Selection([("restoring", "Restoring"), ("done", "Done"), ("failed", "Failed")],
                                     readonly=True)
    kept_database = fields.Char(readonly=True, help="The database as it was before the rollback, kept a few days.")
    line_ids = fields.One2many("mtbg.github.update", "batch_id")

    _name_unique = models.Constraint("UNIQUE(name)", "One batch per name.")

    @api.model
    def _mtbg_get(self, name):
        return self.sudo().search([("name", "=", (name or "").removesuffix("-back"))], limit=1)


class MtbgGithubUpdate(models.Model):
    _name = "mtbg.github.update"
    _description = "Extension update"
    _order = "id desc"
    _rec_name = "module"

    batch = fields.Char(string="Batch Name", index=True, readonly=True, help="Extensions updated together.")
    batch_id = fields.Many2one("mtbg.github.batch", readonly=True, ondelete="cascade", index=True)
    module = fields.Char(required=True, readonly=True)
    kind = fields.Selection([("update", "Update"), ("install", "Install"), ("revert", "Revert"),
                             ("rollback", "Automatic rollback"), ("adopt", "Take over"), ("imported", "Earlier")],
                            required=True, readonly=True)
    from_version = fields.Char(readonly=True, help="Empty: the copy after the store in addons_path.")
    to_version = fields.Char(readonly=True, help="Empty: the copy after the store in addons_path.")
    previous_target = fields.Char(readonly=True, help="The store folder the module loaded from before (empty: none).")
    target = fields.Char(readonly=True)
    state = fields.Selection([
        ("restarting", "Restarting"), ("applied", "Done"), ("failed", "Failed, switched back"),
        ("rolled_back", "Rolled back"), ("cancelled", "Cancelled")], required=True, default="restarting", readonly=True)
    switched_at = fields.Float(readonly=True, help="When the code was switched (epoch seconds).")
    message = fields.Text(readonly=True)
    required_by = fields.Char(readonly=True, help="Installed or updated because this extension's new version needs it")
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    release_id = fields.Many2one("mtbg.github.release", readonly=True, ondelete="set null")

    def _register_hook(self):
        """A server restarted by an update: out of maintenance."""
        super()._register_hook()
        maintenance.clear_after_restart(self.env.cr.dbname)

    def _mtbg_commit(self):
        """Each step is kept even if the server restarts right after (not inside tests)."""
        from odoo.modules import module as odoo_module
        if not odoo_module.current_test:
            self.env.cr.commit()

    # ------------------------------------------------------------------ checks before anything changes
    @api.model
    def _mtbg_check_ready(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only administrators can update extensions."))
        owner = self._mtbg_owner_problem()
        if owner:
            raise UserError(owner)
        problem = reexec.restart_problem(self.env)
        if problem:
            raise UserError(problem)
        if not store.in_addons_path():
            raise UserError(_("This server does not load updated versions yet. Put this folder FIRST in addons_path "
                              "of the server configuration, then restart:\n%s", store.active_dir()))
        busy = self._mtbg_busy_elsewhere()
        if busy:
            raise UserError(busy)

    @api.model
    def _mtbg_owner_problem(self):
        """Why this database may not use the store (another database's), or ""."""
        uuid_ = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        try:
            return store.claim(self.env.cr.dbname, uuid_)
        except store.StoreError as error:
            return str(error)

    @api.model
    def _mtbg_busy_elsewhere(self):
        """Why no batch may start now: one of ours still running, or one of the Tours / CFD updater's (while they are
        still installed) or their maintenance."""
        if self.search_count([("state", "=", "restarting")]):
            return _("Another update is still running.")
        if self.env["mtbg.github.batch"].search_count([("restore_state", "=", "restoring")]):
            return _("A rollback is still running.")
        cr = self.env.cr
        for table, label in (("mtbg_update_log", "Tours"), ("cfd_update_log", "Communication Flow Designer")):
            cr.execute("SELECT to_regclass(%s)", (table,))
            if cr.fetchone()[0]:
                cr.execute("SELECT count(*) FROM %s WHERE state = 'restarting'" % table)  # noqa: S608 - fixed names
                if cr.fetchone()[0]:
                    return _("The %s updater is still running an update.", label)
        if maintenance.active(cr.dbname):
            return _("The site is being updated.")
        return ""

    @api.model
    def _mtbg_check_no_downgrade(self, everything):
        """An update never puts a version older than (or the same as) the one this site has into the store, first in
        addons_path: it would hide the newer code (only Go back does that, on purpose)."""
        names = [m for m, r, _b in everything if r]
        modules = {m.name: m for m in self.env["ir.module.module"].sudo().search(
            [("name", "in", names), ("state", "in", ("installed", "to upgrade"))])}
        older = []
        for module, release, _by in everything:
            current = modules[module]._mgu_current() if release and module in modules else ""
            if current and store.version_key(release.version) <= store.version_key(current):
                older.append(_("%(module)s %(version)s (this site has %(current)s)", module=module,
                               version=release.version, current=current))
        if older:
            raise UserError(_("No update was made: these are not newer than what this site has:\n%s", "\n".join(older)))

    @api.model
    def _mtbg_check_shadows(self, modules):
        problems = []
        for module in modules:
            folder = store.shadowed_by(module)
            if folder:
                problems.append(_("%(module)s: %(folder)s comes before the store in addons_path and has its own copy "
                                  "(Odoo would load that one).", module=module, folder=folder))
        if problems:
            raise UserError(_("No update was made:\n%s", "\n".join(problems)))

    # ------------------------------------------------------------------ start
    @api.model
    def _mtbg_start(self, plan, kind="update", package=None, title=""):
        """``plan``: [(module name, release or None for the copy after the store)]; ``kind`` "install": modules this
        site does not have yet. Returns the progress action."""
        self._mtbg_check_ready()
        if not plan:
            raise UserError(_("Choose at least one extension."))
        self.env["ir.module.module"].check_access("write")
        # a release may depend on a module this site does not have yet (from any source): it comes in the same batch
        if kind != "revert":
            self._mtbg_check_no_downgrade([(m, r, None) for m, r in plan])  # before anything is read or changed
        everything = self._mtbg_full_plan(plan) if kind in ("update", "install", "package") else \
            [(m, r, None) for m, r in plan]
        required = everything[len(plan):]
        if kind != "revert":
            self._mtbg_check_no_downgrade(everything)
        self._mtbg_check_shadows([m for m, r, _b in everything if r])
        name = uuid.uuid4().hex[:12]
        dbname = self.env.cr.dbname
        live = not self.env.context.get("mtbg_no_restart")
        if live:
            # maintenance from before the backup: nothing a visitor enters can be lost by a rollback to it
            maintenance.start(dbname, name)
        try:
            backup, snapshot = store.backup_database(dbname, name)
        except (store.StoreError, OSError, subprocess.SubprocessError) as error:
            if live:
                maintenance.stop(dbname)
            raise UserError(_("No update was made: the database backup failed (%s).", error)) from error
        header = self.env["mtbg.github.batch"].create({
            "name": name, "kind": kind, "package_id": package.id if package else False,
            "title": title or (package.name if package else ""), "backup_path": backup, "snapshot_path": snapshot,
            "backup_at": fields.Datetime.now()})
        installed = set(self.env["ir.module.module"].sudo().search(
            [("name", "in", [m for m, _r, _b in required]), ("state", "in", ("installed", "to upgrade"))]).mapped("name"))
        on_site = self._mtbg_installed()
        logs = self.browse()
        done = []
        try:
            for module, release, required_by in everything:
                before = store.active_target(module)
                if release:
                    source = release.source_id
                    target = store.install_version(source.code, source.url, source.subdir or "", module,
                                                   release.version, release.tag)
                else:
                    target = None
                store.switch(module, target)
                done.append((module, before))
                if required_by:
                    line_kind = "update" if module in installed else "install"
                elif kind in ("install", "package"):
                    line_kind = "update" if module in on_site else "install"
                else:
                    line_kind = kind
                logs |= self.create({
                    "batch": name, "batch_id": header.id, "module": module, "kind": line_kind,
                    "required_by": required_by or False,
                    "release_id": release.id if release else False,
                    "from_version": self._mtbg_version_of(before, module),
                    "to_version": release.version if release else False,
                    "previous_target": before or False, "target": target or False,
                    "switched_at": time.time(),
                })
        except (store.StoreError, OSError) as error:
            for module, before in reversed(done):
                store.switch(module, before)
            if live:
                maintenance.stop(dbname)
            raise UserError(_("No update was made: %s", error)) from error
        self._mtbg_commit()
        logs._mtbg_upgrade_then_restart()
        return {"type": "ir.actions.client", "tag": "mtbg_github_progress", "params": {"batch": name},
                "name": _("Updating")}

    @api.model
    def _mtbg_full_plan(self, plan):
        """Everything a batch brings: [(module, release or None, why)], the plan first (why None). Then, until
        nothing more comes: the new dependencies (from any source), the glues the batch completes, and the modules
        RELEASED TOGETHER with a release it takes (its package's other modules this site has at an older version: a
        release is made to run with them, e.g. a page-templates glue with the catalog that gave it its model)."""
        entries = [(module, release, None) for module, release in plan]
        while True:
            names = {module for module, _r, _w in entries}
            base = [(module, release) for module, release, _w in entries]
            required = self._mtbg_new_dependencies(base)
            required += self._mtbg_auto_installs(base, required)
            more = [entry for entry in required if entry[0] not in names]
            names |= {module for module, _r, _w in more}
            siblings = self._mtbg_released_with([release for _m, release, _w in entries + more if release], names)
            if not more and not siblings:
                return entries
            entries += more + siblings

    @api.model
    def _mtbg_released_with(self, releases, names):
        """The modules released together with ``releases`` (their packages' other modules) that this site has at an
        older version and that are not in ``names``: [(module, release, why)]."""
        Package = self.env["mtbg.github.package"]
        rows = []
        for package in Package.search([("release_ids", "in", [r.id for r in releases])]):
            for module, release, what in package._mtbg_plan():
                if what == "update" and module not in names:
                    names.add(module)
                    rows.append((module, release, _("released with “%s”", package.name)))
        return rows

    @api.model
    def _mtbg_installed(self):
        return set(self.env["ir.module.module"].sudo().search([("state", "in", ("installed", "to upgrade"))]).mapped("name"))

    @api.model
    def _mtbg_new_dependencies(self, plan):
        """Modules the planned versions need that this site has not installed (or runs too old): [(module, release or
        None (the server's own copy), required by)], deepest last, from ANY source (a new dependency may come from
        another repository). Three reasons:

        * a planned version depends on it (its manifest's ``depends``);
        * an installed module is split: its manifest's ``mtbg_tours_split`` ({version: [modules]}) lists the
          extensions that take over part of it from that version on; an update across that version brings them;
        * a planned version needs a dependency at a minimum version (``mtbg_requires`` / ``mtbg_tours_requires``:
          {module: version}): an installed one running older is updated in the same batch (its newest release); a
          new one must be at least that version.

        A module neither on the server nor released, or no release recent enough, stops the update before anything
        changes."""
        from odoo.modules.module import get_module_path
        Module = self.env["ir.module.module"].sudo()
        Release = self.env["mtbg.github.release"].sudo()
        sources = self.env["mtbg.github.source"].search([])
        installed_modules = Module.search([("state", "in", ("installed", "to upgrade"))])
        installed = set(installed_modules.mapped("name"))
        running = {m.name: m._mgu_current() or "" for m in installed_modules}
        planned = {module for module, _release in plan}
        planned_versions = {module: release.version if release else "" for module, release in plan}
        queue = [(module, release) for module, release in plan if release]
        required = []
        while queue:
            module, release = queue.pop(0)
            try:
                manifest = release._mtbg_manifest()
            except store.StoreError as error:
                raise UserError(_("No update was made: %s", error)) from error
            needs = [(dep, module) for dep in manifest.get("depends") or []]
            minimum = manifest_requires(manifest)
            needs += [(dep, module) for dep in minimum if dep not in dict(needs)]
            if module in installed:
                now, to = store.version_key(running.get(module)), store.version_key(release.version)
                for version, parts in sorted((manifest.get(SPLIT_KEY) or {}).items()):
                    if now < store.version_key(version) <= to:
                        needs += [(part, _("%(module)s %(version)s (split)", module=module, version=version))
                                  for part in parts or []]
            for dep, by in needs:
                least = minimum.get(dep) if by == module else None
                if dep in planned:
                    chosen = planned_versions.get(dep)
                    if least and chosen is not None and store.version_key(chosen) < store.version_key(least):
                        raise UserError(_("No update was made: %(module)s %(version)s needs %(dep)s %(least)s or later "
                                          "(chosen: %(chosen)s).", module=module, version=release.version, dep=dep,
                                          least=least, chosen=chosen or _("the server's copy")))
                    continue
                if dep in installed and not (least and store.version_key(running.get(dep)) < store.version_key(least)):
                    continue
                planned.add(dep)
                newest = Release.search([("module", "=", dep), ("source_id", "in", sources.ids)],
                                        order="version_sort desc", limit=1)
                if least and newest and store.version_key(newest.version) < store.version_key(least):
                    newest = Release
                if newest:
                    planned_versions[dep] = newest.version
                    required.append((dep, newest, by))
                    queue.append((dep, newest))
                elif not least and dep not in installed and get_module_path(dep, display_warning=False):
                    required.append((dep, None, by))  # on the server already, only not installed
                elif least:
                    raise UserError(_("No update was made: %(module)s %(version)s needs %(dep)s %(least)s or later, "
                                      "which no update source releases. Check GitHub first (or add its source).",
                                      module=module, version=release.version, dep=dep, least=least)
                                    + self.env["mtbg.github.source"]._mtbg_known_hint())
                else:
                    raise UserError(_("No update was made: %(module)s %(version)s needs %(dep)s, which this site does "
                                      "not have and no update source releases. Check GitHub first (or add its "
                                      "source).", module=module, version=release.version, dep=dep)
                                    + self.env["mtbg.github.source"]._mtbg_known_hint())
        return required

    @api.model
    def _mtbg_auto_installs(self, plan, required):
        """Glue modules that come with this batch: [(module, release, why)], with what they need themselves.

        A glue module (its manifest's ``auto_install``: True, or the list of modules that bring it) installs itself
        in Odoo once all those modules are installed, but only when its code is on the server: on a site the
        updater manages its code comes from the store, so the updater brings it. From the sources' newest releases:

        * not installed: installed in the same batch when every module that brings it is installed or in the batch;
        * installed: kept on its newest release when one of its dependencies is updated in the batch;

        each with its own new dependencies and minimum versions. A glue whose needs no source can meet is left out
        (logged): it never stops the update it would only come along with. A retired glue (gone from the newest
        branch head holding its release, its old tags kept) never comes."""
        from .source import RETIRED
        Module = self.env["ir.module.module"].sudo()
        installed_modules = Module.search([("state", "in", ("installed", "to upgrade"))])
        installed = set(installed_modules.mapped("name"))
        running = {m.name: m._mgu_current() or "" for m in installed_modules}
        sources = self.env["mtbg.github.source"].search([])
        newest = {}
        for release in self.env["mtbg.github.release"].sudo().search([("source_id", "in", sources.ids)],
                                                                     order="version_sort desc"):
            newest.setdefault(release.module, release)
        batch = [(module, release) for module, release in plan] + [(module, release) for module, release, _by in required]
        in_batch = {module for module, _release in batch}
        updated = in_batch & installed
        glues, changed, cache = [], True, {}
        while changed:  # a glue may bring another one
            changed = False
            for module, release in sorted(newest.items()):
                if module in in_batch or module in RETIRED:
                    continue
                source = release.source_id
                try:
                    manifest = release._mtbg_manifest()
                except store.StoreError:
                    continue
                auto = manifest.get("auto_install")
                if not auto or not manifest.get("installable", True):
                    continue
                if not store.still_released(source.code, source.url, source.subdir or "", module, release.commit, cache):
                    continue
                depends = [dep for dep in manifest.get("depends") or []]
                triggers = auto if isinstance(auto, (list, tuple)) else depends
                if module in installed:
                    if not (set(depends) & updated) or \
                            store.version_key(release.version) <= store.version_key(running.get(module)):
                        continue
                    why = _("kept with %s", " + ".join(sorted(set(depends) & updated)))
                elif set(depends) & in_batch and all(dep in installed or dep in in_batch for dep in depends) \
                        and all(dep in installed or dep in in_batch for dep in triggers):
                    # (only with a batch that brings or updates one of its modules, and every one of them there)
                    why = _("installed by itself, needed by %s",
                            " + ".join(dep for dep in triggers if dep != "base") or module)
                else:
                    continue
                try:
                    extra = self._mtbg_new_dependencies(batch + [(module, release)])
                except UserError as error:
                    _logger.warning("Updates: %s %s not brought along: %s", module, release.version, error)
                    continue
                glues.append((module, release, why))
                batch.append((module, release))
                in_batch.add(module)
                for dep, dep_release, by in extra:
                    if dep not in in_batch:
                        glues.append((dep, dep_release, by))
                        in_batch.add(dep)
                        if dep_release:
                            batch.append((dep, dep_release))
                changed = True
        return glues

    @api.model
    def _mtbg_version_of(self, folder, module):
        """The version a module ran before: its store folder's name, else the installed version."""
        if folder:
            return os.path.basename(os.path.dirname(folder))
        installed = self.env["ir.module.module"].sudo().search([("name", "=", module)], limit=1)
        return installed.latest_version or False

    def _mtbg_upgrade_then_restart(self):
        """After the answer reached the browser: upgrade the database in a process of its own, then restart (or, if
        the upgrade failed, switch back: the automatic rollback). Without restarts (tests): only the restart step."""
        if self.env.context.get("mtbg_no_restart"):
            return self._mtbg_trigger_and_restart()
        dbname, log_ids = self.env.cr.dbname, self.ids
        registry = self.env.registry
        new = self.filtered(lambda log: log.kind == "install").mapped("module")
        modules = self.env["ir.module.module"].sudo().search(
            [("name", "in", self.filtered(lambda l: l.kind != "adopt").mapped("module")), ("name", "not in", new),
             ("state", "=", "installed")]).mapped("name")
        batch = self[:1].batch or ""

        def work():
            time.sleep(1.5)
            restarting = False
            try:
                if modules or new:
                    maintenance.start(dbname, batch)
                    maintenance.wait_for_jobs(dbname)
                ok, output = store.upgrade_database(
                    dbname, modules, install=new, before_try=lambda: maintenance.end_sessions(dbname),
                ) if modules or new else (True, "")
            except (store.StoreError, OSError, subprocess.SubprocessError) as error:
                ok, output = False, str(error)
            except Exception as error:  # noqa: BLE001 - never leave the site in maintenance
                _logger.exception("Updates: the upgrade could not run")
                ok, output = False, str(error)
            try:
                restarting = self._mtbg_after_upgrade(registry, log_ids, ok, output, new, modules)
            finally:
                if not restarting:
                    maintenance.stop(dbname)  # (a restarted server clears it when it loads)
        threading.Thread(target=work, daemon=True).start()

    def _mtbg_after_upgrade(self, registry, log_ids, ok, output, new, modules):
        """The upgrade's result: restart with the new code, or switch back (which restarts too). Returns whether a
        restart was asked for."""
        restore = None
        with registry.cursor() as cr:
            logs = self.with_env(self.env(cr=cr)).browse(log_ids)
            if not ok:
                _logger.error("Updates: the upgrade of %s failed:\n%s", ", ".join(modules), output)
                restore = logs._mtbg_prepare_restore(output)
                cr.commit()
        if restore:
            # outside any cursor of this database: it is swapped for the backup's
            if self.env["mtbg.github.batch"]._mtbg_restore_work(registry, *restore):
                return True  # restored; the server restarts (in maintenance until then)
        with registry.cursor() as cr:
            logs = self.with_env(self.env(cr=cr)).browse(log_ids)
            if ok:
                logs.write({"message": _("Database upgraded; restarting.")})
                logs._mtbg_finish_after_restart()
                cr.commit()
                _logger.info("Updates: %s upgraded, restarting the server", ", ".join(new + modules))
                from odoo.service import server
                server.restart()
                return True
            logs._mtbg_rollback(_("The upgrade failed; the previous version is back.\n%s", output))
            cr.commit()
            return bool(logs.search_count([("batch", "in", [b + "-back" for b in logs.mapped("batch")]),
                                           ("state", "=", "restarting")]))

    def _mtbg_prepare_restore(self, output):
        """A failed upgrade may have changed the database already (the process of its own commits module by module:
        a migration that ran cannot be undone by the old code), so the batch's backup comes back: the database, the
        files and the code, as before the batch; then the server restarts. Returns (plan, journal) for
        ``_mtbg_restore_work``, or None without a backup (the code alone goes back then)."""
        header = self[:1].batch_id
        if not header or not header.backup_path or not os.path.isfile(header.backup_path):
            return None
        message = _("The upgrade failed: the database, the files and the code are back as before it.\n%s", output)
        self.write({"state": "failed", "message": message[-4000:]})
        header.message = message[-2000:]
        Batch = self.env["mtbg.github.batch"]
        dbname = self.env.cr.dbname
        stamp = time.strftime("%Y%m%d%H%M%S")
        history = Batch._mtbg_history_rows(header)
        history.append({"batch": {"name": "rb-" + stamp, "kind": "restore", "title": _("Automatic rollback of %s",
                                                                                           header.title or header.name),
                                  "backup_path": "", "snapshot_path": "", "backup_at": None, "message": "",
                                  "kept_database": "%s_before_rollback_%s" % (dbname[:30], stamp),
                                  "created": fields.Datetime.to_string(fields.Datetime.now())}, "lines": []})
        plan = {"restore": "rb-" + stamp, "of": header.name, "database": dbname, "dump": header.backup_path,
                "snapshot": header.snapshot_path or "", "restored": "%s_restore_%s" % (dbname[:40], stamp),
                "keep_as": "%s_before_rollback_%s" % (dbname[:30], stamp), "user": "auto",
                "links": {log.module: log.previous_target or None for log in self},
                "undone": [header.name], "history": history, "failed": True}
        journal = Batch._mtbg_journal(dbname, plan["restore"], dict(plan, state="started"))
        return plan, journal

    @api.model
    def _mtbg_trigger_and_restart(self):
        self._mtbg_finish_after_restart()
        self._mtbg_commit()
        if self.env.context.get("mtbg_no_restart"):
            return

        def restart():
            time.sleep(1.5)
            from odoo.service import server
            _logger.info("Updates: restarting the server to load the new versions")
            server.restart()
        threading.Thread(target=restart, daemon=True).start()

    # ------------------------------------------------------------------ apply (in a process started after the switch)
    @api.model
    def _mtbg_batch(self, batch):
        """The batch the progress screen follows. Odoo often reloads the page after the restart (new assets) and the
        reloaded screen can come back without it: then the newest batch still restarting, else the newest one."""
        if batch:
            return batch.removesuffix("-back")
        log = self.sudo().search([("state", "=", "restarting")], limit=1) or self.sudo().search([], limit=1)
        return (log.batch or "").removesuffix("-back") or False

    @api.model
    def mtbg_apply(self, batch):
        """Finish a batch once the server restarted with its code (progress screen, Finish update).
        Returns "waiting" (not restarted yet), "busy" (Odoo is running a scheduled job: try again) or "done"."""
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only administrators can update extensions."))
        batch = self._mtbg_batch(batch)
        if not batch:
            return "done"
        logs = self.search([("batch", "in", [batch, batch + "-back"]), ("state", "=", "restarting")])
        if not logs:
            return "done"
        if store.PROCESS_STARTED < max(logs.mapped("switched_at")):
            return "waiting"
        for sub in set(logs.mapped("batch")):
            result = logs.filtered(lambda l: l.batch == sub)._mtbg_apply()
            if result != "done":
                return result
        return "done"

    def action_finish(self):
        """History: finish an update whose progress screen was closed."""
        state = self.mtbg_apply(self[:1].batch.removesuffix("-back"))
        if state == "waiting":
            raise UserError(_("The server has not restarted since this update: restart it, then Finish update."))
        if state == "busy":
            raise UserError(_("Odoo is running a scheduled job: try again in a minute."))
        return {"type": "ir.actions.client", "tag": "reload"}

    def _mtbg_busy(self):
        """Is Odoo running a scheduled job (module operations would be refused)? Locks the jobs if not."""
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute("SET LOCAL lock_timeout = '3s'")
                self.env.cr.execute("SELECT id FROM ir_cron FOR UPDATE NOWAIT")
            return False
        except Exception:  # noqa: BLE001 - a lock error: a job is running
            return True

    def _mtbg_apply(self):
        Module = self.env["ir.module.module"].sudo()
        modules = Module.search([("name", "in", self.filtered(lambda l: l.kind != "adopt").mapped("module")),
                                 ("state", "=", "installed")])
        # upgraded already by the process of its own: only mark them done
        targets = {log.module: log.to_version for log in self}
        modules = modules.filtered(lambda m: not targets.get(m.name) or m.latest_version != targets[m.name])
        names = self.filtered(lambda log: log.kind == "install").mapped("module")
        if set(names) - set(Module.search([("name", "in", names)]).mapped("name")):
            Module.update_list()  # a module this database has never seen
        new = Module.search([("name", "in", names), ("state", "!=", "installed")])
        if (modules or new) and self._mtbg_busy():
            return "busy"
        try:
            if new:
                new.button_immediate_install()
            if modules:
                modules.button_immediate_upgrade()
        except Exception as error:  # noqa: BLE001 - any failure: the backup comes back (or the code alone)
            from odoo.modules import module as odoo_module
            if not odoo_module.current_test:
                self.env.cr.rollback()
            _logger.exception("Updates: upgrade failed, going back")
            logs = self.filtered(lambda l: l.kind != "rollback")
            restore = logs._mtbg_prepare_restore(str(error)) if logs and not odoo_module.current_test else None
            if restore:
                self.env.cr.commit()
                maintenance.start(self.env.cr.dbname, self[:1].batch or "")
                registry, Batch = self.env.registry, self.env["mtbg.github.batch"]
                threading.Thread(target=Batch._mtbg_restore_work, args=(registry, *restore), daemon=True).start()
                return "failed"
            self._mtbg_rollback(str(error))
            return "failed"
        self._mtbg_commit()
        for log in self:
            if log.kind == "adopt":
                message = _("Taken over: loaded from this store.")
            elif log.kind != "install":
                message = _("Upgraded.")
            elif log.required_by:
                message = _("Installed (required by %s).", log.required_by)
            else:
                message = _("Installed.")
            log.write({"state": "applied", "message": message})
        self._mtbg_commit()
        return "done"

    def _mtbg_rollback(self, error):
        rollback = self.browse()
        for log in self:
            if log.kind in ("rollback", "adopt"):
                # a failed rollback keeps the version that worked before; a take-over has nothing to go back to
                log.write({"state": "failed", "message": error[-4000:]})
                continue
            store.switch(log.module, log.previous_target or None)
            log.write({"state": "failed", "message": error[-4000:]})
            rollback |= self.create({
                "batch": log.batch + "-back", "batch_id": log.batch_id.id, "module": log.module, "kind": "rollback",
                "from_version": log.to_version, "to_version": log.from_version,
                "previous_target": log.target, "target": log.previous_target, "switched_at": time.time()})
        self._mtbg_commit()
        if rollback:
            self._mtbg_trigger_and_restart()

    @api.model
    def _mtbg_finish_after_restart(self):
        """Nobody may be watching the progress screen (a production server): the new server finishes the batch by
        itself, from its scheduled job, shortly after the restart (twice, should the restart be slow)."""
        cron = self.env.ref("mtbg_github_updates.ir_cron_finish_updates", raise_if_not_found=False)
        if cron:
            now = fields.Datetime.now()
            cron.sudo()._trigger(at=[now + timedelta(seconds=30), now + timedelta(minutes=3)])

    @api.model
    def _cron_finish_restarted(self):
        """Scheduled job: mark done the batches the restarted server runs, when nothing is left to do but that (the
        database upgraded by the process of its own, or a rollback's code back on the version the database has).
        Batches that still need a module operation wait for an administrator (progress screen, Finish update)."""
        logs = self.sudo().search([("state", "=", "restarting"), ("switched_at", "<", store.PROCESS_STARTED)])
        Module = self.env["ir.module.module"].sudo()
        for batch in set(logs.mapped("batch")):
            batch_logs = logs.filtered(lambda l: l.batch == batch)
            modules = {m.name: m for m in Module.search([("name", "in", batch_logs.mapped("module"))])}
            if not all(self._mtbg_line_ready(log, modules.get(log.module)) for log in batch_logs):
                _logger.info("Updates: batch %s needs an administrator to finish it", batch)
                continue
            result = batch_logs._mtbg_apply()
            _logger.info("Updates: batch %s finished after the restart (%s)", batch, result)

    @api.model
    def _mtbg_line_ready(self, log, module):
        """Nothing left for a module operation after the restart: a take-over (the same code, nothing to upgrade), a
        module this database does not have installed (a store link of an uninstalled or retired one), an install the
        process of its own made, an upgrade it made (the database at the new version)."""
        if log.kind == "adopt":
            return True
        if log.kind == "install":
            return bool(module) and module.state == "installed"
        if not module or module.state == "uninstalled":
            return True
        return module.state == "installed" and (not log.to_version or module.latest_version == log.to_version)

    # ------------------------------------------------------------------ the progress screen
    @api.model
    def mtbg_progress(self, batch):
        """Each extension of the batch (and its automatic rollback); no batch (a page reloaded after the restart):
        the batch still running."""
        batch = self._mtbg_batch(batch)
        if not batch:
            return []
        logs = self.sudo().search([("batch", "in", [batch, batch + "-back"])], order="id")
        return [{"module": l.module, "kind": l.kind,
                 "from": _("new") if l.kind == "install" else l.from_version or "—",
                 "to": l.to_version or "—", "state": l.state,
                 "message": l.message or (_("Required by %s.", l.required_by) if l.required_by else "")} for l in logs]

    def action_cancel(self):
        """A batch stuck before its restart (the server was not restarted): switch back and forget it."""
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only administrators can manage updates."))
        for log in self.filtered(lambda l: l.state == "restarting"):
            store.switch(log.module, log.previous_target or None)
            log.write({"state": "cancelled", "message": _("Cancelled; the previous version is back (restart the server).")})
        return True
