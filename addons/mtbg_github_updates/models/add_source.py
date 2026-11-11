"""Add a repository (0.4.1, owner 2026-11-11: "with the new updater I see no way to add a new repository"): one form,
opened from the Extensions board, the Activity screen and Sources. One of the organization's repositories this site
does not read yet (the list), or any GitHub repository by its address."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MtbgGithubSourceAdd(models.TransientModel):
    _name = "mtbg.github.source.add"
    _description = "Add a repository"

    def _known_selection(self):
        return [(k["key"], "%s (%s)" % (k["name"], k["url"]))
                for k in self.env["mtbg.github.source"]._mtbg_known_missing()] + [("other", _("Another repository"))]

    known = fields.Selection(selection="_known_selection", string="Repository", required=True,
                             default=lambda self: self._known_selection()[0][0])
    name = fields.Char(help="How the board and the Activity screen name it, e.g. Bike Shop.")
    url = fields.Char(string="GitHub address", help="git@github.com:owner/repository.git")
    subdir = fields.Char(string="Modules folder", default="addons",
                         help="The repository's folder holding the modules (empty: its top).")

    def action_add(self):
        self.ensure_one()
        Source = self.env["mtbg.github.source"]
        if self.known == "other":
            source_id = Source.mtbg_add_repository(self.name, self.url, self.subdir or "")
        else:
            source_id = Source.mtbg_add_known(self.known)
        source = Source.browse(source_id)
        # straight on to its access: the deploy key to give GitHub, then Test connection
        if not source.has_key:
            try:
                source.action_generate_key()
            except Exception:  # noqa: BLE001 - the key can still be made on the form
                pass
        return source.action_mgu_access()

    @api.model
    def action_mgu_open(self, *args):
        """The form, from a list or board header button (called with the selected records, or none)."""
        self.env["mtbg.github.source"]._mtbg_check_admin()
        return {"type": "ir.actions.act_window", "res_model": self._name, "view_mode": "form", "target": "new",
                "views": [[self.env.ref("mtbg_github_updates.view_mtbg_github_source_add_form").id, "form"]],
                "name": _("Add a repository")}
