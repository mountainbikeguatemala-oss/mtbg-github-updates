from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import store


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    # (mgu_: the Tours updater's own mtbg_* fields stay while both are installed)
    mgu_source_id = fields.Many2one("mtbg.github.source", string="GitHub Source", compute="_compute_mgu")
    mgu_running_version = fields.Char(string="Version Running", compute="_compute_mgu",
                                      help="The version the server loads: from the update store, else the installed one.")
    mgu_newest_release_id = fields.Many2one("mtbg.github.release", string="Newest Release", compute="_compute_mgu")
    mgu_update = fields.Boolean(string="Newer Release on GitHub", compute="_compute_mgu", search="_search_mgu_update")
    mgu_in_store = fields.Boolean(string="Loaded from the store", compute="_compute_mgu")

    def _mgu_loaded(self):
        """(version of the code Odoo loads for this module, whether it is the store's), from the copy found first in
        addons_path, not from a store link that may not be the one loaded (a dev site's work in progress before it)."""
        import os
        from odoo.modules.module import Manifest
        manifest = Manifest.for_addon(self.name, display_warning=False) if self.name else None
        if not manifest:
            return "", False
        here = os.path.realpath(manifest.addons_path) == os.path.realpath(store.active_dir())
        return manifest.get("version") or "", here

    def _mgu_current(self):
        """The version this site has: the newest of the code loaded and the database's (never offer older)."""
        loaded, _here = self._mgu_loaded()
        return max(loaded or "", self.latest_version or "", key=store.version_key) or False

    def _compute_mgu(self):
        Source = self.env["mtbg.github.source"]
        Release = self.env["mtbg.github.release"]
        sources = Source.search([])
        for module in self:
            name = module.name or ""
            # its newest release from any source that may offer it (a module that moved repository: its new one's,
            # though the installed manifest is still the old one's)
            newest = Release.search([("module", "=", name), ("source_id", "in", sources.ids)],
                                    order="version_sort desc", limit=1) if name else Release
            source = newest.source_id or (Source._mtbg_source_of(name) if name and module.state != "uninstalled"
                                          else Source)
            module.mgu_source_id = source
            loaded, here = module._mgu_loaded() if store.MODULE_RE.match(name) else ("", False)
            module.mgu_in_store = here
            module.mgu_running_version = module._mgu_current()
            module.mgu_newest_release_id = newest
            module.mgu_update = bool(
                newest and module.state == "installed"
                and store.version_key(newest.version) > store.version_key(module.mgu_running_version))

    @api.model
    def action_mgu_open_updates(self, patterns=None, title=None):
        """The Updates page, on a family of extensions (``patterns``) or on these modules."""
        from .page import clean_patterns
        patterns = clean_patterns(patterns or self.mapped("name"))
        return {"type": "ir.actions.client", "tag": "mtbg_github_updates", "name": title or _("Updates"),
                "params": {"modules": patterns, "title": title or ""}}

    def _search_mgu_update(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            raise UserError(_("Unsupported search."))
        names = set(self.env["mtbg.github.release"].search([]).mapped("module"))
        candidates = self.search([("name", "in", list(names)), ("state", "=", "installed")])
        ids = candidates.filtered("mgu_update").ids
        return [("id", "in" if (operator == "=") == value else "not in", ids)]
