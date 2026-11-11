"""Off-site backups: a daily snapshot of the database and its files in Cloudflare R2, kept on a fading schedule.

The R2 side goes through Cloudflare Connector (``cloudflare.r2``, a soft check: no dependency). In the bucket:

    snapshots/<db>/<YYYYMMDD-HHMMSS>/database.dump   pg_dump, custom format
    snapshots/<db>/<YYYYMMDD-HHMMSS>/files.json      the snapshot's file list: [[path, size], ...]
    files/<db>/<xx>/<sha1>                          each uploaded file once (Odoo names them by their content)

A file is uploaded the first time a snapshot needs it, and is shared by every snapshot after that. So thirty
snapshots cost about the same as one: the files once, plus a dump each. A file is deleted only when no kept
snapshot lists it any more (and it is older than the bucket lock).

Fading: the rules in ``mtbg.offsite.rule`` (owner, 2026-11-10: "we should have adjustable schedule for backups with
schedules for daily, weekly, monthly etc and expire after"). Each rule keeps one snapshot every N days (0 = every
snapshot) until it expires after M days (0 = never). A snapshot follows the first rule it has not outlived; within
each period the oldest snapshot stays, so a kept snapshot keeps its place as it ages. The default rules: every day
for 7 days, every 7 days to 30, every 30 to 60, every 60 to 180, every 180 to 360, every 360 days forever.

How often a snapshot is taken is a setting too: every N hours, days or weeks (days and weeks: from the set hour).

A run that does not finish within its time budget (a first upload of several GB) saves where it got to and asks
the job to go on a minute later; the files already in R2 are not sent again.
"""
import datetime
import json
import logging
import os
import tempfile
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import store

_logger = logging.getLogger(__name__)

PRICE_PER_GB_MONTH = 0.015  # R2 Standard storage (USD), after the account's free 10 GB (Cloudflare's price list)
FREE_GB = 10
TIME_BUDGET = 420  # seconds of uploading per job run (the cron's own limit is longer)
LOCK_DAYS = 7  # the bucket lock Set up Cloudflare puts on snapshots/ and files/ (nothing younger can be deleted)

DEFAULT_RULES = [  # (name, keep one every N days (0: every one), expire after M days (0: never))
    ("Daily", 1, 7),
    ("Weekly", 7, 30),
    ("Monthly", 30, 60),
    ("Every 60 days", 60, 180),
    ("Every 180 days", 180, 360),
    ("Yearly (kept forever)", 360, 0),
]


def rule_for(age_days, rules):
    """The rule a snapshot of this age follows: the first it has not outlived (``rules``: [(id, name, every,
    expire)] sorted by expire, never last). None: every rule has expired it."""
    for rule in rules:
        if not rule[3] or age_days < rule[3]:
            return rule
    return None


def fade(snapshots, now, rules=None):
    """Which snapshots the rules keep. ``snapshots``: [(id, taken_at)]; ``rules``: [(id, name, every, expire)]
    (default: DEFAULT_RULES). Returns {snapshot id: rule name} for the kept ones; the others are to be removed."""
    if rules is None:
        rules = [(i, n, e, x) for i, (n, e, x) in enumerate(DEFAULT_RULES)]
    rules = sorted(rules, key=lambda r: (r[3] == 0, r[3]))
    kept, best = {}, {}
    for sid, taken in snapshots:
        rule = rule_for((now - taken).total_seconds() / 86400.0, rules)
        if rule is None:
            continue
        if not rule[2]:
            kept[sid] = rule[1]
            continue
        slot = (rule[0], int(taken.timestamp() // 86400) // rule[2])
        if slot not in best or taken < best[slot][0]:
            best[slot] = (taken, sid, rule[1])
    for _slot, (_taken, sid, name) in best.items():
        kept[sid] = name
    return kept


def simulate(rules, every_hours, horizon_days=3 * 365):
    """How many snapshots the rules keep after ``horizon_days`` of taking one every ``every_hours`` hours (the
    page's preview)."""
    now = datetime.datetime(2030, 1, 1)
    step = datetime.timedelta(hours=max(1, every_hours))
    snaps, t, i = [], now - datetime.timedelta(days=horizon_days), 0
    while t <= now:
        snaps.append((i, t))
        t += step
        i += 1
    return len(fade(snaps, now, rules))


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024


class MtbgOffsiteBackup(models.Model):
    _name = "mtbg.offsite.backup"
    _description = "Off-site backup (Cloudflare R2)"
    _order = "taken_at desc, id desc"
    _rec_name = "stamp"

    stamp = fields.Char(readonly=True, required=True, help="The snapshot's folder in the bucket (UTC time).")
    taken_at = fields.Datetime("Taken", readonly=True, required=True)
    tier = fields.Char("Kind", readonly=True, default="New")
    state = fields.Selection([("running", "Running"), ("verified", "Verified"), ("failed", "Failed")],
                             readonly=True, default="running")
    message = fields.Char(readonly=True)
    bucket = fields.Char(readonly=True)
    db_size = fields.Float("Database", readonly=True, digits=(16, 0))
    files_size = fields.Float("Files", readonly=True, digits=(16, 0))
    files_count = fields.Integer("Files (count)", readonly=True)
    files_new = fields.Integer("New files", readonly=True, help="Files this snapshot was the first to upload.")
    total_size = fields.Float("Total", compute="_compute_total", digits=(16, 0))
    db_size_label = fields.Char("Database ", compute="_compute_labels")
    files_size_label = fields.Char("Files ", compute="_compute_labels")
    total_size_label = fields.Char("Total ", compute="_compute_labels")
    files_done = fields.Integer(readonly=True)  # (progress of a run that goes on in the next job)

    def _compute_total(self):
        for rec in self:
            rec.total_size = rec.db_size + rec.files_size

    def _compute_labels(self):
        for rec in self:
            rec.db_size_label = human_size(rec.db_size)
            rec.files_size_label = "%s (%s new)" % (human_size(rec.files_size), rec.files_new)
            rec.total_size_label = human_size(rec.db_size + rec.files_size)

    # ------------------------------------------------------------------ R2
    @api.model
    def _mtbg_r2(self):
        if "cloudflare.r2" not in self.env:
            raise UserError(_("Off-site backups need Cloudflare Connector: install it, then Settings › Cloudflare › "
                              "Set up Cloudflare."))
        R2 = self.env["cloudflare.r2"].sudo()
        if not R2.is_configured():
            raise UserError(_("Cloudflare R2 is not set up yet: Settings › Cloudflare › Set up Cloudflare."))
        return R2.client()

    def _prefix(self):
        self.ensure_one()
        return "snapshots/%s/%s/" % (self.env.cr.dbname, self.stamp)

    @api.model
    def _files_prefix(self):
        return "files/%s/" % self.env.cr.dbname

    # ------------------------------------------------------------------ buttons
    @api.model
    def action_backup_now(self):
        """Back up now: the job starts in a moment (an upload can take longer than a page request may)."""
        self._mtbg_check_admin()
        self._mtbg_r2()  # (says at once when R2 is not set up)
        cron = self.env.ref("mtbg_github_updates.ir_cron_offsite_backup")
        cron.sudo()._trigger()
        self.env["ir.config_parameter"].sudo().set_param("mtbg_github_updates.offsite_requested", "1")
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "type": "info", "title": _("Off-site backup"),
            "message": _("The backup starts in a moment and appears in the list when it is verified."),
            "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    def action_download_database(self):
        return self._mtbg_download("database.dump", "%s-%s.dump" % (self.env.cr.dbname, self.stamp))

    def action_download_files_list(self):
        return self._mtbg_download("files.json", "%s-%s-files.json" % (self.env.cr.dbname, self.stamp))

    def _mtbg_download(self, name, filename):
        self.ensure_one()
        self._mtbg_check_admin()
        if self.state != "verified":
            raise UserError(_("Only a verified backup can be downloaded."))
        url = self.env["cloudflare.r2"].sudo().download_url(self._prefix() + name, 3600, filename,
                                                            bucket=self.bucket or None)
        # (self: a download in the same tab; a new tab opened after a server call is blocked as a popup)
        return {"type": "ir.actions.act_url", "url": url, "target": "self"}

    @api.model
    def _mtbg_check_admin(self):
        if not self.env.su and not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only administrators can manage off-site backups."))

    # ------------------------------------------------------------------ the job
    @api.model
    def _cron_offsite(self):
        """The daily job (and the continuation of a run that ran out of time, and Back up now)."""
        ICP = self.env["ir.config_parameter"].sudo()
        running = self.search([("state", "=", "running")], order="id desc", limit=1)
        requested = ICP.get_param("mtbg_github_updates.offsite_requested")
        if not running and not requested and not self._mtbg_due():
            return
        if requested:
            ICP.set_param("mtbg_github_updates.offsite_requested", "")
        try:
            client = self._mtbg_r2()
        except UserError as e:
            _logger.warning("Off-site backup skipped: %s", e)
            return
        record = running or self._mtbg_start(client)
        if not record:
            return
        try:
            finished = record._mtbg_continue(client)
        except Exception as e:  # noqa: BLE001 - every failure is shown on the backup
            _logger.exception("Off-site backup %s failed", record.stamp)
            record.write({"state": "failed", "message": str(e)[:500]})
            return
        if not finished:
            self.env.ref("mtbg_github_updates.ir_cron_offsite_backup").sudo()._trigger(
                at=fields.Datetime.now() + datetime.timedelta(minutes=1))
            return
        self._mtbg_retention(client)

    @api.model
    def _mtbg_frequency(self):
        """(every, unit, hour): a snapshot every ``every`` hours / days / weeks; days and weeks from ``hour``."""
        ICP = self.env["ir.config_parameter"].sudo()
        try:
            every = max(1, int(ICP.get_param("mtbg_github_updates.offsite_every") or 1))
        except ValueError:
            every = 1
        unit = ICP.get_param("mtbg_github_updates.offsite_unit") or "days"
        if unit not in ("hours", "days", "weeks"):
            unit = "days"
        try:
            hour = float(ICP.get_param("mtbg_github_updates.offsite_hour") or 2.5)
        except ValueError:
            hour = 2.5
        return every, unit, hour

    @api.model
    def _mtbg_every_hours(self):
        every, unit, _hour = self._mtbg_frequency()
        return every * {"hours": 1, "days": 24, "weeks": 168}[unit]

    @api.model
    def _mtbg_due(self):
        """A snapshot is due: switched on, the last one (verified or still running) older than the frequency (a
        little slack, so a daily one does not creep later every day), and, for days and weeks, the site's hour
        reached."""
        ICP = self.env["ir.config_parameter"].sudo()
        if ICP.get_param("mtbg_github_updates.offsite_enabled") in (False, None, "", "0", "False", "false"):
            return False
        every, unit, hour = self._mtbg_frequency()
        hours = self._mtbg_every_hours()
        slack = min(4.0, hours * 0.2)
        recent = self.search([("state", "in", ("verified", "running")),
                              ("taken_at", ">", fields.Datetime.now() - datetime.timedelta(hours=hours - slack))],
                             limit=1)
        if recent:
            return False
        if unit == "hours":
            return True
        tz = self.env.ref("base.user_admin").tz or "UTC"
        local = fields.Datetime.context_timestamp(self.with_context(tz=tz), fields.Datetime.now())
        return local.hour + local.minute / 60.0 >= hour

    @api.model
    def _mtbg_start(self, client):
        now = fields.Datetime.now()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        record = self.create({"stamp": stamp, "taken_at": now, "bucket": client.bucket})
        self.env.cr.commit()  # (the row is there while the dump and the upload go on)
        return record

    def _mtbg_continue(self, client):
        """Do what is left of this snapshot within the time budget. True when it is verified."""
        self.ensure_one()
        start = time.monotonic()
        prefix = self._prefix()
        dbname = self.env.cr.dbname
        if not self.db_size:
            with tempfile.TemporaryDirectory(prefix="mtbg-offsite-") as tmp:
                dump = os.path.join(tmp, "database.dump")
                store._pg("pg_dump", "--format=custom", "--no-owner", "--file", dump, dbname)
                client.put_file(prefix + "database.dump", dump)
                self.write({"db_size": os.path.getsize(dump), "message": _("Database uploaded; files next")})
            self.env.cr.commit()
        root = store.filestore_dir(dbname)
        listing = []
        if os.path.isdir(root):
            for folder, _dirs, names in os.walk(root):
                for name in names:
                    full = os.path.join(folder, name)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    if rel.startswith("checklist") or name.startswith("."):
                        continue
                    listing.append((rel, os.path.getsize(full)))
        listing.sort()
        remote = {o["key"][len(self._files_prefix()):]: o["size"] for o in client.list(self._files_prefix())}
        new = self.files_new
        for i, (rel, size) in enumerate(listing):
            if remote.get(rel) == size:
                continue
            client.put_file(self._files_prefix() + rel, os.path.join(root, rel))
            new += 1
            if new % 200 == 0:
                self.write({"files_new": new, "files_done": i, "message": _("Uploading files: %(i)s of %(n)s",
                                                                             i=i, n=len(listing))})
                self.env.cr.commit()
            if time.monotonic() - start > TIME_BUDGET:
                self.write({"files_new": new, "files_done": i, "message": _(
                    "Uploading files: %(i)s of %(n)s (goes on in a minute)", i=i, n=len(listing))})
                self.env.cr.commit()
                return False
        manifest = json.dumps([[rel, size] for rel, size in listing], separators=(",", ":")).encode()
        client.put_bytes(prefix + "files.json", manifest, "application/json")
        # verified: the dump and the list are in R2 with the sizes sent, and every listed file is there
        dump_head = client.head(prefix + "database.dump")
        if not dump_head or dump_head["size"] != int(self.db_size):
            raise UserError(_("The database dump in R2 is not the one sent."))
        remote = {o["key"][len(self._files_prefix()):]: o["size"] for o in client.list(self._files_prefix())}
        missing = [rel for rel, size in listing if remote.get(rel) != size]
        if missing:
            raise UserError(_("%(n)s files are not in R2 (first: %(f)s)", n=len(missing), f=missing[0]))
        self.write({"state": "verified", "files_count": len(listing), "files_new": new,
                    "files_size": sum(size for _rel, size in listing), "message": False})
        self.env.cr.commit()
        _logger.info("Off-site backup %s verified (%s files, %s new)", self.stamp, len(listing), new)
        return True

    # ------------------------------------------------------------------ fading + clean-up
    @api.model
    def _mtbg_retention(self, client):
        """Fade the verified snapshots; remove the dropped ones and the files no kept snapshot lists."""
        now = fields.Datetime.now()
        verified = self.search([("state", "=", "verified")])
        kept = fade([(r.id, r.taken_at) for r in verified], now, self.env["mtbg.offsite.rule"]._mtbg_rules())
        for rec in verified:
            if rec.id in kept:
                if rec.tier != kept[rec.id]:
                    rec.tier = kept[rec.id]
            elif (now - rec.taken_at).days >= LOCK_DAYS:
                for obj in client.list(rec._prefix()):
                    client.delete(obj["key"])
                rec.unlink()
        # failed runs older than a week: their partial folders go too
        for rec in self.search([("state", "=", "failed"),
                                ("taken_at", "<", now - datetime.timedelta(days=LOCK_DAYS))]):
            for obj in client.list(rec._prefix()):
                client.delete(obj["key"])
            rec.unlink()
        self.env.cr.commit()
        # files no kept snapshot lists (and older than the lock)
        wanted = set()
        for rec in self.search([("state", "in", ("verified", "running"))]):
            try:
                wanted.update(rel for rel, _size in json.loads(client.get_bytes(rec._prefix() + "files.json")))
            except Exception:  # noqa: BLE001 - a running snapshot has no list yet: keep every file
                if rec.state == "running":
                    return
                raise
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=LOCK_DAYS)
        for obj in client.list(self._files_prefix()):
            rel = obj["key"][len(self._files_prefix()):]
            modified = obj.get("modified")
            try:
                when = datetime.datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None
            except ValueError:
                when = None
            if rel not in wanted and when and when < cutoff:
                client.delete(obj["key"])

    # ------------------------------------------------------------------ totals (settings, list)
    @api.model
    def mtbg_storage_summary(self):
        """{'used': bytes in the bucket, 'used_label', 'monthly': USD, 'monthly_label', 'snapshots': n}. The
        price: R2 Standard after the free 10 GB, which the whole Cloudflare account shares."""
        try:
            client = self._mtbg_r2()
            used = sum(o["size"] for o in client.list(""))
        except Exception as e:  # noqa: BLE001
            return {"used": 0, "used_label": "—", "monthly": 0.0, "monthly_label": str(e)[:120],
                    "snapshots": self.search_count([("state", "=", "verified")])}
        gb = used / 1024 ** 3
        monthly = max(0.0, gb - FREE_GB) * PRICE_PER_GB_MONTH
        return {"used": used, "used_label": human_size(used), "monthly": monthly,
                "monthly_label": ("$%.2f / month" % monthly) if monthly >= 0.005 else _("$0.00 / month (in the "
                                                                                      "free 10 GB)"),
                "snapshots": self.search_count([("state", "=", "verified")])}


class MtbgOffsiteRule(models.Model):
    """One line of the fading schedule: keep one snapshot every N days, until it expires after M days."""
    _name = "mtbg.offsite.rule"
    _description = "Off-site backups: a fading rule"
    _order = "forever, expire_days, id"

    name = fields.Char("Name", required=True, help="Shown as the snapshot's kind, e.g. Weekly.")
    every_days = fields.Integer("Every (days)", required=True, default=7,
                                help="Keep one snapshot every this many days. 0: keep every snapshot while this rule applies.")
    expire_days = fields.Integer("Expires (days)", required=True, default=30,
                                 help="The age at which the next rule takes over. 0: never (kept forever); only the "
                                      "last rule can be never.")
    forever = fields.Boolean(compute="_compute_forever", store=True)
    summary = fields.Char(compute="_compute_summary", string="Rule")

    @api.depends("expire_days")
    def _compute_forever(self):
        for rule in self:
            rule.forever = not rule.expire_days

    def _compute_summary(self):
        for rule in self:
            every = self.env._("every snapshot") if not rule.every_days else self.env._(
                "one every %s day(s)", rule.every_days)
            until = self.env._("forever") if not rule.expire_days else self.env._(
                "until %s days old", rule.expire_days)
            rule.summary = "%s: %s, %s" % (rule.name, every, until)

    @api.constrains("every_days", "expire_days")
    def _check_rule(self):
        for rule in self:
            if rule.every_days < 0 or rule.expire_days < 0:
                raise UserError(self.env._("Days cannot be negative."))
            if rule.expire_days and rule.expire_days < LOCK_DAYS:
                raise UserError(self.env._("A rule cannot expire before %s days: the bucket lock keeps every file "
                                           "that long.", LOCK_DAYS))
            if rule.expire_days and rule.every_days >= rule.expire_days:
                raise UserError(self.env._("%s: keep one every %s days cannot be as long as its expiry (%s days).",
                                           rule.name, rule.every_days, rule.expire_days))
        if self.search_count([("expire_days", "=", 0)]) > 1:
            raise UserError(self.env._("Only one rule can keep its snapshots forever (expire after 0 = never)."))
        expiries = [r.expire_days for r in self.search([("expire_days", ">", 0)])]
        if len(expiries) != len(set(expiries)):
            raise UserError(self.env._("Two rules expire at the same age: give each a different expiry."))

    @api.ondelete(at_uninstall=False)
    def _unlink_keep_one(self):
        if not self.search_count([("id", "not in", self.ids)]):
            raise UserError(self.env._("Keep at least one rule (with none, every snapshot would be removed)."))

    @api.model
    def _mtbg_rules(self):
        rules = [(r.id, r.name, r.every_days, r.expire_days) for r in self.sudo().search([])]
        return rules or [(i, n, e, x) for i, (n, e, x) in enumerate(DEFAULT_RULES)]

    @api.model
    def action_reset_defaults(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(self.env._("Only administrators can change the backup schedule."))
        self.sudo().search([]).with_context(mtbg_offsite_reset=True)._reset_unlink()
        for name, every, expire in DEFAULT_RULES:
            self.sudo().create({"name": name, "every_days": every, "expire_days": expire})
        return True

    def _reset_unlink(self):
        # (the keep-one check is for a person deleting rows; the reset puts the defaults back at once)
        self.env.cr.execute("DELETE FROM mtbg_offsite_rule WHERE id = ANY(%s)", [self.ids])
        self.env.invalidate_all()


class MtbgOffsitePanel(models.TransientModel):
    """Settings › Off-site backups: the totals, the schedule and every snapshot, on one page."""
    _name = "mtbg.offsite.panel"
    _description = "Off-site backups"
    _rec_name = "title"

    title = fields.Char(default=lambda self: self.env._("Off-site backups"), readonly=True)

    offsite_enabled = fields.Boolean("Back up every day", compute="_compute_panel", inverse="_inverse_schedule")
    offsite_every = fields.Integer("Take a snapshot every", compute="_compute_panel", inverse="_inverse_schedule")
    offsite_unit = fields.Selection([("hours", "hours"), ("days", "days"), ("weeks", "weeks")], string="Unit",
                                    compute="_compute_panel", inverse="_inverse_schedule")
    offsite_hour = fields.Float("At (site time)", compute="_compute_panel", inverse="_inverse_schedule",
                                help="For days and weeks: the hour of the day, in the administrator's time zone.")
    rules_text = fields.Text("Kept", compute="_compute_panel")
    preview = fields.Char("With these rules", compute="_compute_panel")
    bucket = fields.Char(compute="_compute_panel")
    used_label = fields.Char("Storage used", compute="_compute_panel",
                             help="Everything in this site's backup bucket (each file counted once).")
    monthly_label = fields.Char("Current charges", compute="_compute_panel",
                                help="Cloudflare R2 Standard: $0.015 per GB-month after the free 10 GB, which every "
                                     "bucket of the Cloudflare account shares.")
    snapshot_count = fields.Integer("Snapshots kept", compute="_compute_panel")
    schedule_note = fields.Char(compute="_compute_panel")
    backup_ids = fields.Many2many("mtbg.offsite.backup", compute="_compute_panel", string="Backups")

    def _compute_panel(self):
        ICP = self.env["ir.config_parameter"].sudo()
        Backup = self.env["mtbg.offsite.backup"]
        summary = Backup.mtbg_storage_summary()
        bucket = (ICP.get_param("cloudflare_connector.r2_bucket") or "").strip() or _("not set up")
        try:
            hour = float(ICP.get_param("mtbg_github_updates.offsite_hour") or 2.5)
        except ValueError:
            hour = 2.5
        on = ICP.get_param("mtbg_github_updates.offsite_enabled") not in (False, None, "", "0", "False", "false")
        every, unit, _h = Backup._mtbg_frequency()
        Rule = self.env["mtbg.offsite.rule"]
        rules = Rule._mtbg_rules()
        rules_text = "\n".join(Rule.browse([r[0] for r in rules]).exists().mapped("summary")) or ""
        kept_n = simulate(rules, Backup._mtbg_every_hours())
        last = Backup.search([("state", "=", "verified")], limit=1)
        if last:
            est = last.files_size + kept_n * last.db_size
            preview = _("about %(n)s snapshots kept (≈ %(size)s, with today's sizes)", n=kept_n, size=human_size(est))
        else:
            preview = _("about %s snapshots kept", kept_n)
        for panel in self:
            panel.offsite_enabled = on
            panel.offsite_every = every
            panel.offsite_unit = unit
            panel.rules_text = rules_text
            panel.preview = preview
            panel.offsite_hour = hour
            panel.bucket = bucket
            panel.used_label = summary["used_label"]
            panel.monthly_label = summary["monthly_label"]
            panel.snapshot_count = summary["snapshots"]
            panel.schedule_note = _("Older snapshots fade by the rules below; nothing younger than %s days is ever "
                                    "removed (the bucket lock).", LOCK_DAYS)
            panel.backup_ids = Backup.search([])

    def _inverse_schedule(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for panel in self:
            ICP.set_param("mtbg_github_updates.offsite_enabled", "1" if panel.offsite_enabled else "0")
            ICP.set_param("mtbg_github_updates.offsite_hour", "%.2f" % min(max(panel.offsite_hour, 0.0), 23.99))
            ICP.set_param("mtbg_github_updates.offsite_every", str(max(1, panel.offsite_every or 1)))
            ICP.set_param("mtbg_github_updates.offsite_unit", panel.offsite_unit or "days")

    def action_backup_now(self):
        return self.env["mtbg.offsite.backup"].action_backup_now()

    def action_refresh(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "view_mode": "form", "target": "current",
                "res_id": self.create({}).id, "name": _("Off-site backups")}

    def action_edit_rules(self):
        return {"type": "ir.actions.act_window", "res_model": "mtbg.offsite.rule", "name": _("Backup schedule"),
                "view_mode": "list", "target": "current",
                "views": [(self.env.ref("mtbg_github_updates.mtbg_offsite_rule_list").id, "list")]}

    @api.model
    def action_open(self):
        return self.action_refresh()
