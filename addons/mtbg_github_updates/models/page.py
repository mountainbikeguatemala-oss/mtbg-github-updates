"""The one Updates page: a section per source (its packages, its modules, what is new on GitHub), the store's setup,
the take-over from the earlier updaters, and the history with its rollbacks. Data and buttons only: the page is
static/src/updates."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import reexec, store
from .source import RETIRED

KINDS_UNDOABLE = ("update", "install", "package", "revert")


class MtbgGithubSource(models.Model):
    _inherit = "mtbg.github.source"

    @api.model
    def mtbg_updates_page(self):
        self._mtbg_check_admin()
        # a batch the restarted server runs, nobody on its progress screen and the jobs paused: finished here too
        self.env["mtbg.github.update"]._cron_finish_restarted()
        self._mtbg_sync_declared()
        Module = self.env["ir.module.module"]
        sources = self.search([])
        installed = Module.search([("state", "=", "installed")])
        by_source = {}
        for module in installed:
            if module.mgu_source_id:
                by_source.setdefault(module.mgu_source_id.id, Module)
                by_source[module.mgu_source_id.id] |= module
        sections = []
        for source in sources:
            modules = by_source.get(source.id, Module).sorted("name")
            sections.append({
                "id": source.id, "name": source.name, "url": source.url, "has_key": source.has_key,
                "has_token": source.has_token, "public_key": source.public_key or "", "declared": source.declared,
                "https": (source.url or "").startswith("https://"),
                "last_check": fields.Datetime.to_string(source.last_check) if source.last_check else False,
                "last_error": source.last_error or "", "releases": source.release_count,
                "modules": [self._mtbg_module_row(m) for m in modules],
                "packages": [self._mtbg_package_row(p) for p in source.package_ids[:8]],
                "available": self._mtbg_available(source),
            })
        Batch = self.env["mtbg.github.batch"]
        adopt = Batch.mtbg_adopt_preview()
        return {
            "owner_problem": self.env["mtbg.github.update"]._mtbg_owner_problem(),
            "restart_problem": reexec.restart_problem(self.env) or "",
            "store": {"ready": store.in_addons_path(), "active": store.active_dir(), "root": store.root()},
            "adopt": {"todo": adopt["todo"],
                      "updaters": [{"label": u["label"], "count": sum(1 for m in u["modules"] if not m["problem"])}
                                   for u in adopt["updaters"] if u["modules"]],
                      "left": [{"module": m["module"], "why": m["problem"]} for u in adopt["updaters"]
                               for m in u["modules"] if m["problem"] and not m["done"]]},
            "sections": sections,
            "history": [self._mtbg_batch_row(b) for b in Batch.search([("kind", "!=", "imported")], limit=20)],
        }

    @api.model
    def _mtbg_module_row(self, m):
        newest = m.mgu_newest_release_id
        revert = self.env["mtbg.github.update"]._mtbg_revert_line(m.name)
        return {"id": m.id, "name": m.name, "title": m.shortdesc or m.name, "summary": m.summary or "",
                "icon": m.icon or "/base/static/description/icon.png", "running": m.mgu_running_version or "",
                "in_store": m.mgu_in_store, "newest": newest.version or "", "release_id": newest.id or False,
                "notes": newest._mtbg_text() if newest else "", "update": m.mgu_update,
                "revert": revert.from_version if revert and revert.kind == "update" else False}

    @api.model
    def _mtbg_package_row(self, package):
        rows = package._mtbg_plan()
        return {"id": package.id, "name": package.name, "tag": package.tag or "", "notes": package.notes or "",
                "date": fields.Datetime.to_string(package.date) if package.date else False,
                "modules": [{"module": module, "version": release.version, "what": what,
                             "notes": release._mtbg_text()} for module, release, what in rows],
                "todo": sum(1 for _m, _r, what in rows if what == "update")}

    @api.model
    def _mtbg_available(self, source):
        """Extensions of ``source`` released on GitHub that this site has not installed: the newest of each."""
        installed = set(self.env["ir.module.module"].search([("state", "in", ("installed", "to upgrade"))]).mapped("name"))
        newest = {}
        for release in source.release_ids.sorted("version_sort", reverse=True):
            if release.module not in installed and release.module not in newest and release.module not in RETIRED:
                newest[release.module] = release
        rows, cache = [], {}
        for module, release in sorted(newest.items()):
            if not store.still_released(source.code, source.url, source.subdir or "", module, release.commit, cache):
                continue
            try:
                manifest = release._mtbg_manifest()
            except store.StoreError:
                continue
            if not manifest.get("installable", True) or manifest.get("auto_install") or manifest.get("category") == "Hidden":
                continue  # a glue comes by itself with what it links; a hidden one is an empty hand-over shell
            rows.append({"id": release.id, "name": module, "version": release.version,
                         "title": (manifest.get("name") or module).split("—")[-1].strip(),
                         "summary": manifest.get("summary") or "", "notes": release._mtbg_text()})
        return rows

    @api.model
    def _mtbg_batch_row(self, batch):
        kinds = dict(batch._fields["kind"]._description_selection(self.env))
        states = dict(self.env["mtbg.github.update"]._fields["state"]._description_selection(self.env))
        lines = batch.line_ids.sorted("id")
        if batch.kind == "restore":
            state = batch.restore_state or "done"
        elif batch.undone:
            state = "undone"
        elif lines.filtered(lambda l: l.state == "restarting"):
            state = "restarting"
        elif lines.filtered(lambda l: l.state == "failed"):
            state = "failed"
        else:
            state = "applied" if lines else "empty"
        return {"id": batch.id, "name": batch.name, "kind": kinds.get(batch.kind, batch.kind),
                "title": batch.title or "", "date": fields.Datetime.to_string(batch.create_date),
                "user": batch.user_id.name or "", "state": state, "message": batch.message or "",
                "lines": [{"module": l.module, "kind": l.kind, "from": l.from_version or "", "to": l.to_version or "",
                           "state": l.state, "state_label": states.get(l.state, l.state), "message": l.message or ""}
                          for l in lines],
                "can_roll_back": batch.kind in KINDS_UNDOABLE and not batch._mtbg_rollback_problem()}

    # ------------------------------------------------------------------ what a button would do, then do it
    @api.model
    def mtbg_plan(self, kind, ids):
        """The confirmation's content: the extensions of the batch (with what comes with them), or why not.
        ``kind``: "modules" (installed module ids: their newest release), "install" (a release id), "package"."""
        self._mtbg_check_admin()
        Update = self.env["mtbg.github.update"]
        plan, package = self._mtbg_plan_of(kind, ids)
        try:
            required = Update._mtbg_new_dependencies(plan)
            glues = Update._mtbg_auto_installs(plan, required)
        except UserError as error:
            return {"ok": False, "problem": str(error.args[0] if error.args else error)}
        rows = [{"module": m, "version": r.version if r else "", "why": ""} for m, r in plan]
        rows += [{"module": m, "version": r.version if r else _("(on this server)"), "why": by} for m, r, by in required + glues]
        shadows = [_("%(module)s: %(folder)s comes before the store and has its own copy.", module=row["module"],
                     folder=store.shadowed_by(row["module"])) for row in rows if store.shadowed_by(row["module"])]
        return {"ok": not shadows, "problem": "\n".join(shadows), "rows": rows,
                "skipped": [{"module": m, "version": r.version, "what": what} for m, r, what in
                            (package._mtbg_plan() if package else []) if what != "update"]}

    @api.model
    def _mtbg_plan_of(self, kind, ids):
        ids = [int(i) for i in (ids or [])]
        if kind == "modules":
            modules = self.env["ir.module.module"].browse(ids).exists().filtered("mgu_update")
            if not modules:
                raise UserError(_("No newer version on GitHub. Use Check GitHub first."))
            return [(m.name, m.mgu_newest_release_id) for m in modules], None
        if kind == "all":
            modules = self.env["ir.module.module"].search([("mgu_update", "=", True)])
            if not modules:
                raise UserError(_("No newer version on GitHub. Use Check GitHub first."))
            return [(m.name, m.mgu_newest_release_id) for m in modules], None
        if kind == "install":
            release = self.env["mtbg.github.release"].browse(ids[:1]).exists()
            if not release:
                raise UserError(_("This release is no longer listed: Check GitHub again."))
            return [(release.module, release)], None
        if kind == "package":
            package = self.env["mtbg.github.package"].browse(ids[:1]).exists()
            if not package:
                raise UserError(_("This release is no longer listed: Check GitHub again."))
            plan = [(m, r) for m, r, what in package._mtbg_plan() if what == "update"]
            if not plan:
                raise UserError(_("Every module of this release is already at its version (or not on this site)."))
            return plan, package
        raise UserError(_("Unknown update."))

    @api.model
    def mtbg_start(self, kind, ids):
        """The confirmed batch: backup, store, switch, upgrade in a process of its own, restart."""
        self._mtbg_check_admin()
        plan, package = self._mtbg_plan_of(kind, ids)
        return self.env["mtbg.github.update"]._mtbg_start(
            plan, kind={"install": "install", "package": "package"}.get(kind, "update"), package=package)
