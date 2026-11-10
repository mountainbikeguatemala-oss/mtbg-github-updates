from odoo import _, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    mtbg_offsite_enabled = fields.Boolean("Off-site backups every day",
                                          config_parameter="mtbg_github_updates.offsite_enabled")
    mtbg_offsite_hour = fields.Float("At (site time)", config_parameter="mtbg_github_updates.offsite_hour",
                                     default=2.5)
    mtbg_offsite_summary = fields.Char("Storage", compute="_compute_mtbg_offsite_summary")

    def _compute_mtbg_offsite_summary(self):
        summary = self.env["mtbg.offsite.backup"].mtbg_storage_summary()
        text = _("%(used)s used · %(cost)s · %(n)s snapshots kept", used=summary["used_label"],
                 cost=summary["monthly_label"], n=summary["snapshots"])
        for rec in self:
            rec.mtbg_offsite_summary = text

    def action_mtbg_offsite_open(self):
        return self.env["mtbg.offsite.panel"].action_open()
