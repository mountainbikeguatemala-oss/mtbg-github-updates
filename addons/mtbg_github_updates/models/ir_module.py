from odoo import _, fields, models
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

    def _compute_mgu(self):
        Source = self.env["mtbg.github.source"]
        Release = self.env["mtbg.github.release"]
        sources = Source.search([])
        for module in self:
            name = module.name or ""
            source = Source._mtbg_source_of(name) if name and module.state != "uninstalled" else Source
            module.mgu_source_id = source
            running = store.active_version(name) if store.MODULE_RE.match(name) else None
            module.mgu_in_store = bool(running)
            module.mgu_running_version = running or module.latest_version or False
            newest = Release.search([("module", "=", name), ("source_id", "in", (source or sources).ids)],
                                    order="version_sort desc", limit=1) if name else Release
            module.mgu_newest_release_id = newest
            module.mgu_update = bool(
                newest and module.state == "installed"
                and store.version_key(newest.version) > store.version_key(module.mgu_running_version))

    def _search_mgu_update(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            raise UserError(_("Unsupported search."))
        names = set(self.env["mtbg.github.release"].search([]).mapped("module"))
        candidates = self.search([("name", "in", list(names)), ("state", "=", "installed")])
        ids = candidates.filtered("mgu_update").ids
        return [("id", "in" if (operator == "=") == value else "not in", ids)]
