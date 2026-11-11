"""The extensions board (0.4.0, owner 2026-11-11: "kanban style view for different sources / extensions and their
dependencies and a list view with a side filter"; "different columns for different sources, card / list view").

Odoo's own kanban, list, form and search panel on ``ir.module.module`` (the updater must run on a bare site and update
itself: no other module). One column per source; each card an extension with the version running and the newest on
GitHub, its status, the folder its code loads from, what it needs and what uses it, across repositories. The columns
and filters read stored fields, refreshed when the board opens and after Check GitHub (``_mgu_board_refresh``). Updates
themselves run from the Activity screen (the confirmation, the backup, the restart and its progress live there).
"""
import os
import re

from odoo import _, api, fields, models

from . import store

STATUSES = [("update", "Update"), ("new", "New"), ("current", "Up to date"), ("local", "Not from GitHub"),
            ("error", "Error")]
FOLDERS = [("store", "Updates store"), ("addons", "Addons folder"), ("odoo", "Odoo")]
REPO_RE = re.compile(r"^(?:git@github\.com:|https://github\.com/)(?P<path>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$")


def _odoo_roots():
    """Odoo's own addons folders (the core modules')."""
    import odoo.addons.base as base_module
    roots = {os.path.realpath(os.path.dirname(os.path.dirname(base_module.__file__)))}
    try:
        import odoo.addons.web as web_module
        roots.add(os.path.realpath(os.path.dirname(os.path.dirname(web_module.__file__))))
    except ImportError:
        pass
    return roots


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    mgu_board = fields.Boolean(string="On the Extensions board", index=True, readonly=True)
    mgu_board_source_id = fields.Many2one("mtbg.github.source", string="Source", index=True, readonly=True,
                                          ondelete="set null", help="The repository this extension comes from.")
    mgu_status = fields.Selection(STATUSES, string="Status", readonly=True)
    mgu_board_running = fields.Char(string="Running", readonly=True)
    mgu_board_available = fields.Char(string="On GitHub", readonly=True, help="The newest version released.")
    mgu_folder_kind = fields.Selection(FOLDERS, string="Folder", readonly=True,
                                       help="Where its code loads from: the updater's store, another folder of the "
                                            "server's addons path (the updater cannot update it there), or Odoo.")
    mgu_folder = fields.Char(string="Folder Path", readonly=True)
    mgu_shadowed = fields.Char(string="Hidden Behind", readonly=True,
                               help="A folder before the store in the addons path has its own copy: the store's "
                                    "version is not the one loaded.")
    mgu_needs_ids = fields.Many2many("ir.module.module", string="Needs", compute="_compute_mgu_links")
    mgu_used_by_ids = fields.Many2many("ir.module.module", string="Used By", compute="_compute_mgu_links")
    mgu_release_ids = fields.Many2many("mtbg.github.release", string="Releases", compute="_compute_mgu_links")

    def _compute_mgu_links(self):
        Release = self.env["mtbg.github.release"]
        for module in self:
            module.mgu_needs_ids = module.dependencies_id.depend_id
            module.mgu_used_by_ids = self.search([("dependencies_id.name", "=", module.name),
                                                  ("state", "in", ("installed", "to upgrade"))]) \
                if module.name else self.browse()
            module.mgu_release_ids = Release.search([("module", "=", module.name)]) if module.name else Release

    # ------------------------------------------------------------------ the stored board values
    def _mgu_folder_of(self, roots):
        """(kind, path, shadowed by) of the code Odoo loads for this module."""
        from odoo.modules.module import Manifest
        manifest = Manifest.for_addon(self.name, display_warning=False) if self.name else None
        if not manifest:
            return False, False, False
        folder = os.path.realpath(manifest.addons_path)
        if folder == os.path.realpath(store.active_dir()):
            kind = "store"
        elif folder in roots:
            kind = "odoo"
        else:
            kind = "addons"
        # hidden: the store holds a version of it, but Odoo loads another copy (from a folder before the store)
        shadowed = folder if kind != "store" and store.MODULE_RE.match(self.name) and store.active_version(self.name) \
            else False
        return kind, folder, shadowed

    @api.model
    def _mgu_board_refresh(self):
        """The board's stored values: every installed extension, and the ones released on a source but not
        installed (new)."""
        Release = self.env["mtbg.github.release"].sudo()
        Update = self.env["mtbg.github.update"].sudo()
        released = set(Release.search([]).mapped("module"))
        modules = self.sudo().search(["|", ("state", "in", ("installed", "to upgrade", "to install")),
                                      ("name", "in", list(released))])
        self.sudo().search([("mgu_board", "=", True), ("id", "not in", modules.ids)]).write({
            "mgu_board": False, "mgu_board_source_id": False, "mgu_status": False})
        # (the updater's own computed values, read fresh: releases may have been listed in this transaction)
        modules.invalidate_recordset(["mgu_source_id", "mgu_newest_release_id", "mgu_update", "mgu_running_version",
                                      "mgu_in_store"])
        roots = _odoo_roots()
        last = {}
        for line in Update.search([("module", "in", modules.mapped("name"))], order="id desc"):
            last.setdefault(line.module, line)
        for module in modules:
            installed = module.state in ("installed", "to upgrade", "to install")
            newest = module.mgu_newest_release_id
            source = module.mgu_source_id
            kind, folder, shadowed = module._mgu_folder_of(roots)
            if not installed:
                status = "new"
            elif last.get(module.name) and last[module.name].state in ("failed", "restarting"):
                status = "error"
            elif module.mgu_update:
                status = "update"
            elif source:
                status = "current"
            else:
                status = "local"
            values = {"mgu_board": True, "mgu_board_source_id": source.id or False, "mgu_status": status,
                      "mgu_board_running": (module.mgu_running_version or False) if installed else False,
                      "mgu_board_available": newest.version or False, "mgu_folder_kind": kind,
                      "mgu_folder": folder, "mgu_shadowed": shadowed}
            changed = {k: v for k, v in values.items()
                       if (module[k].id if isinstance(module[k], models.BaseModel) else module[k]) != v}
            if changed:
                module.write(changed)
        return True

    @api.model
    def action_mgu_board(self):
        """Settings › Updates › Extensions: the board, its values refreshed first."""
        self.env["mtbg.github.source"]._mtbg_check_admin()
        self._mgu_board_refresh()
        action = self.env["ir.actions.act_window"]._for_xml_id("mtbg_github_updates.action_mgu_extensions")
        return action

    def action_mgu_open_one(self):
        """The Activity screen on this extension: its update, install or going back, with the confirmation there."""
        self.ensure_one()
        return self.action_mgu_open_updates(patterns=[self.name], title=self.shortdesc or self.name)

    def action_mgu_open_github(self):
        """Its folder on GitHub at the newest release's tag (or the source's front page)."""
        self.ensure_one()
        source = self.mgu_board_source_id
        match = REPO_RE.match((source.url or "").strip()) if source else None
        if not match:
            return False
        url = "https://github.com/%s" % match["path"]
        newest = self.mgu_newest_release_id
        if newest:
            sub = (source.subdir or "").strip("/")
            url += "/tree/%s/%s%s" % (newest.tag, (sub + "/") if sub else "", self.name)
        return {"type": "ir.actions.act_url", "url": url, "target": "new"}


class MtbgGithubSource(models.Model):
    _inherit = "mtbg.github.source"

    def action_check_updates(self):
        result = super().action_check_updates()
        self.env["ir.module.module"]._mgu_board_refresh()
        return result

    def action_generate_key(self):
        """From the source's form (0.4.2: the owner saw nothing happen): the form again, its new public key on it to
        copy; elsewhere as before (a notification)."""
        result = super().action_generate_key()
        if self.env.context.get("mgu_from_form") or self.env.context.get("mgu_show_key"):
            return self._mgu_key_popup()
        return result

    def _mgu_key_popup(self):
        """The new key in a popup (owner, 2026-11-11: "display the key in the popup with a copy button after you
        click new key"): copied at once where the browser allows it, a Copy button, and GitHub's page to add it."""
        self.ensure_one()
        self.invalidate_recordset(["public_key", "has_key"])
        match = REPO_RE.match((self.url or "").strip())
        github = "https://github.com/%s/settings/keys/new" % match["path"] if match else ""
        return {"type": "ir.actions.client", "tag": "mgu_new_key", "target": "new", "name": _("Deploy key: %s", self.name),
                "params": {"key": self.public_key or "", "source": self.name, "github_url": github}}

    def action_mgu_access(self):
        """The column's Access: the source's form (deploy key, address, folder)."""
        self._mtbg_check_admin()
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "mtbg.github.source", "res_id": self.id,
                "views": [[self.env.ref("mtbg_github_updates.view_mtbg_github_source_form").id, "form"]],
                "target": "new", "name": _("Access: %s", self.name)}
