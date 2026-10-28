"""The one Updates page: a section per source (its packages, its modules, what is new on GitHub), the store's setup,
the take-over from the earlier updaters, and the history with its rollbacks. Data and buttons only: the page is
static/src/updates."""
import fnmatch
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import reexec, store
from .source import RETIRED

KINDS_UNDOABLE = ("update", "install", "package", "revert")
PATTERN_RE = re.compile(r"^[a-z0-9_*?]{1,80}$")


def clean_patterns(patterns):
    """A family of extensions the page opens on (e.g. the Logistics page's ["mtbg_tours_logistics*",
    "mtbg_tours_rentals"]): module name patterns, * and ? allowed; empty: everything."""
    patterns = [p.strip() for p in (patterns or []) if isinstance(p, str) and p.strip()]
    if len(patterns) > 30 or not all(PATTERN_RE.match(p) for p in patterns):
        raise UserError(_("Unexpected module filter."))
    return patterns


def matches(name, patterns):
    return not patterns or any(fnmatch.fnmatchcase(name or "", p) for p in patterns)


class MtbgGithubSource(models.Model):
    _inherit = "mtbg.github.source"

    @api.model
    def mtbg_updates_page(self, patterns=None):
        """Everything the page shows; ``patterns``: only a family of extensions (their modules, the packages and new
        releases that have one of them, their history)."""
        self._mtbg_check_admin()
        patterns = clean_patterns(patterns)
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
            modules = by_source.get(source.id, Module).sorted("name").filtered(lambda m: matches(m.name, patterns))
            packages = source.package_ids.filtered(
                lambda p: not patterns or any(matches(r.module, patterns) for r in p.release_ids))[:8]
            available = [a for a in self._mtbg_available(source) if matches(a["name"], patterns)]
            if patterns and not (modules or packages or available):
                continue
            sections.append({
                "id": source.id, "name": source.name, "url": source.url, "has_key": source.has_key,
                "has_token": source.has_token, "public_key": source.public_key or "", "declared": source.declared,
                "https": (source.url or "").startswith("https://"),
                "last_check": fields.Datetime.to_string(source.last_check) if source.last_check else False,
                "last_error": source.last_error or "", "releases": source.release_count,
                "modules": [self._mtbg_module_row(m) for m in modules],
                "packages": [self._mtbg_package_row(p) for p in packages],
                "available": available,
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
            "known": [{"key": k["key"], "name": k["name"], "url": k["url"]} for k in self._mtbg_known_missing()],
            "history": [self._mtbg_batch_row(b) for b in Batch.search([("kind", "!=", "imported")], limit=60)
                        if not patterns or any(matches(m, patterns) for m in b.line_ids.mapped("module"))][:20],
            "patterns": patterns,
        }

    @api.model
    def _mtbg_module_row(self, m):
        newest = m.mgu_newest_release_id
        revert = self.env["mtbg.github.update"]._mtbg_revert_line(m.name)
        return {"id": m.id, "name": m.name, "title": m.shortdesc or m.name, "summary": m.summary or "",
                "icon": m.icon or "/base/static/description/icon.png", "running": m.mgu_running_version or "",
                "in_store": m.mgu_in_store, "newest": newest.version or "", "release_id": newest.id or False,
                "notes": newest._mtbg_text() if newest else "", "update": m.mgu_update,
                "lines": newest._mtbg_lines() if newest else [],
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
            if not manifest.get("installable", True) or manifest.get("category") == "Hidden":
                continue  # a hidden one is an empty hand-over shell
            if manifest.get("auto_install") and not set(manifest.get("depends") or []) <= installed:
                continue  # a glue comes by itself with what it links; once they are all here (it was uninstalled,
                # or is new): offered, it would not come by itself again (owner, 2026-10-28: cfd_website_visitors)
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
            everything = Update._mtbg_full_plan(plan)
            Update._mtbg_check_no_downgrade(everything)
        except UserError as error:
            return {"ok": False, "problem": str(error.args[0] if error.args else error)}
        # what will be installed, one line per change (owner, 2026-10-28)
        rows = [{"module": m, "version": r.version if r else _("(on this server)"), "why": by or "",
                 "lines": r._mtbg_lines() if r else []} for m, r, by in everything]
        shadows = [_("%(module)s: %(folder)s comes before the store and has its own copy.", module=row["module"],
                     folder=store.shadowed_by(row["module"])) for row in rows if store.shadowed_by(row["module"])]
        return {"ok": not shadows, "problem": "\n".join(shadows), "rows": rows,
                "skipped": [{"module": m, "version": r.version, "what": what} for m, r, what in
                            (package._mtbg_plan() if package else [])
                            if what != "update" and m not in {row["module"] for row in rows}]}

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
