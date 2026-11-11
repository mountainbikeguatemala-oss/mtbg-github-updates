import datetime
import json
import os
import tempfile
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import offsite, store


class FakeR2:
    """An in-memory bucket with the R2Client methods the backups use."""

    def __init__(self):
        self.bucket = "test-backups"
        self.objects = {}  # key -> (bytes, modified iso)
        self.puts = 0

    def _now(self):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def put_file(self, key, path, content_type="application/octet-stream"):
        with open(path, "rb") as f:
            self.objects[key] = (f.read(), self._now())
        self.puts += 1

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (data, self._now())
        self.puts += 1

    def get_bytes(self, key):
        return self.objects[key][0]

    def head(self, key):
        return {"size": len(self.objects[key][0]), "etag": ""} if key in self.objects else None

    def delete(self, key):
        self.objects.pop(key, None)

    def list(self, prefix=""):
        return [{"key": k, "size": len(v[0]), "etag": "", "modified": v[1]}
                for k, v in sorted(self.objects.items()) if k.startswith(prefix)]


@tagged("post_install", "-at_install")
class TestOffsiteBackups(TransactionCase):

    def setUp(self):
        super().setUp()
        self.r2 = FakeR2()
        self.tmp = tempfile.mkdtemp(prefix="offsite-test-")
        self.filestore = os.path.join(self.tmp, "filestore")
        for rel, data in (("ab/ab12", b"one"), ("cd/cd34", b"two two"), ("checklist/x", b"skip")):
            os.makedirs(os.path.join(self.filestore, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.filestore, rel), "wb") as f:
                f.write(data)
        patches = [
            patch.object(type(self.env["mtbg.offsite.backup"]), "_mtbg_r2", lambda s: self.r2),
            patch.object(store, "filestore_dir", lambda db: self.filestore),
            patch.object(store, "_pg", self._fake_dump),
            patch.object(self.env.cr, "commit", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.Backup = self.env["mtbg.offsite.backup"]

    def _fake_dump(self, tool, *args, **kw):
        target = args[list(args).index("--file") + 1]
        with open(target, "wb") as f:
            f.write(b"PGDMP fake dump")

    def _run(self):
        rec = self.Backup._mtbg_start(self.r2)
        self.assertTrue(rec._mtbg_continue(self.r2))
        return rec

    def test_first_run_then_only_new_files(self):
        rec = self._run()
        db = self.env.cr.dbname
        self.assertEqual(rec.state, "verified")
        self.assertEqual((rec.files_count, rec.files_new, rec.files_size), (2, 2, 10))
        self.assertIn("snapshots/%s/%s/database.dump" % (db, rec.stamp), self.r2.objects)
        listed = json.loads(self.r2.get_bytes("snapshots/%s/%s/files.json" % (db, rec.stamp)))
        self.assertEqual(listed, [["ab/ab12", 3], ["cd/cd34", 7]], "the checklist folder is not a file")
        self.assertNotIn("files/%s/checklist/x" % db, self.r2.objects)
        os.makedirs(os.path.join(self.filestore, "ef"))
        with open(os.path.join(self.filestore, "ef", "ef56"), "wb") as f:
            f.write(b"three")
        with patch.object(offsite.fields.Datetime, "now",  # (a second snapshot, a second later)
                          return_value=rec.taken_at + datetime.timedelta(seconds=1)):
            second = self._run()
        self.assertEqual((second.files_count, second.files_new), (3, 1), "only the new file is sent")

    def test_time_budget_goes_on_next_run(self):
        rec = self.Backup._mtbg_start(self.r2)
        with patch.object(offsite, "TIME_BUDGET", -1):
            self.assertFalse(rec._mtbg_continue(self.r2), "out of time after the first file")
        self.assertEqual(rec.state, "running")
        self.assertEqual(rec.files_new, 1)
        self.assertTrue(rec._mtbg_continue(self.r2))
        self.assertEqual((rec.state, rec.files_new), ("verified", 2), "the uploaded file is not sent again")

    def test_missing_file_fails_the_check(self):
        rec = self.Backup._mtbg_start(self.r2)
        real_list = self.r2.list
        with patch.object(self.r2, "list", side_effect=[real_list("files/"), []]):
            with self.assertRaises(UserError):
                rec._mtbg_continue(self.r2)

    def test_cron_marks_failures(self):
        self.env["ir.config_parameter"].sudo().set_param("mtbg_github_updates.offsite_requested", "1")
        with patch.object(self.r2, "put_file", side_effect=RuntimeError("R2 down")):
            self.Backup._cron_offsite()
        rec = self.Backup.search([], limit=1)
        self.assertEqual(rec.state, "failed")
        self.assertIn("R2 down", rec.message)

    def test_fade_schedule_over_two_years(self):
        now = datetime.datetime(2028, 11, 10, 2, 30)
        snaps = [(i, now - datetime.timedelta(days=i)) for i in range(0, 730)]
        kept = offsite.fade(snaps, now)
        by_tier = {}
        for sid, tier in kept.items():
            by_tier.setdefault(tier, []).append(sid)
        self.assertEqual(len(by_tier["Daily"]), 7)
        self.assertTrue(3 <= len(by_tier["Weekly"]) <= 4)
        self.assertTrue(1 <= len(by_tier["Monthly"]) <= 2)
        self.assertTrue(2 <= len(by_tier["Every 60 days"]) <= 3)
        self.assertTrue(1 <= len(by_tier["Every 180 days"]) <= 2)
        self.assertTrue(1 <= len(by_tier["Yearly (kept forever)"]) <= 3, "one a year after the first year")
        self.assertLess(len(kept), 25)
        # stable: what is kept today is still kept tomorrow unless a bigger period now covers it
        tomorrow = offsite.fade(snaps, now + datetime.timedelta(days=1))
        self.assertIn(max(by_tier["Yearly (kept forever)"]), tomorrow, "the oldest yearly stays (kept forever)")

    def test_retention_removes_dropped_snapshots_and_unused_files(self):
        old = self._run()
        db = self.env.cr.dbname
        noon = (datetime.datetime.now() - datetime.timedelta(days=20)).replace(hour=12, minute=0, second=0,
                                                                                microsecond=0)
        old.write({"taken_at": noon})  # (both on the same day: the same 7-day period, whatever the date)
        twin = self.Backup.create({"stamp": "20200101-000001", "taken_at": noon + datetime.timedelta(minutes=1),
                                   "state": "verified", "bucket": "test-backups"})
        self.r2.objects["snapshots/%s/20200101-000001/files.json" % db] = (b"[]", self.r2._now())
        self.r2.objects["files/%s/zz/orphan" % db] = (b"x", "2020-01-01T00:00:00.000Z")
        self.r2.objects["files/%s/zz/young-orphan" % db] = (b"x", self.r2._now())
        self.Backup._mtbg_retention(self.r2)
        self.assertTrue(old.exists(), "the oldest of its week stays")
        self.assertFalse(twin.exists(), "the second one of the same week goes")
        self.assertNotIn("files/%s/zz/orphan" % db, self.r2.objects, "an unlisted old file goes")
        self.assertIn("files/%s/zz/young-orphan" % db, self.r2.objects, "the bucket lock: younger than 7 days stays")
        self.assertIn("files/%s/ab/ab12" % db, self.r2.objects)

    def test_download_link_is_private_and_same_tab(self):
        if "cloudflare.r2" not in self.env:
            self.skipTest("Cloudflare Connector not installed in this database")
        rec = self._run()
        with patch.object(type(self.env["cloudflare.r2"]), "download_url",
                          return_value="https://signed.example/x") as link:
            action = rec.action_download_database()
        self.assertEqual(action, {"type": "ir.actions.act_url", "url": "https://signed.example/x", "target": "self"})
        self.assertEqual(link.call_args[0][1:3], (3600, "%s-%s.dump" % (self.env.cr.dbname, rec.stamp)))

    def test_storage_summary_and_charges(self):
        self.r2.objects["files/big"] = (b"0" * 10, self.r2._now())
        with patch.object(offsite, "FREE_GB", 0):
            summary = self.Backup.mtbg_storage_summary()
        self.assertEqual(summary["used"], 10)
        self.assertIn("/ month", summary["monthly_label"])
        summary = self.Backup.mtbg_storage_summary()
        self.assertIn("free 10 GB", summary["monthly_label"])
        panel = self.env["mtbg.offsite.panel"].create({})
        self.assertEqual(panel.used_label, "10 B")

    def test_schedule_due(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("mtbg_github_updates.offsite_enabled", "0")
        self.assertFalse(self.Backup._mtbg_due())
        ICP.set_param("mtbg_github_updates.offsite_enabled", "1")
        ICP.set_param("mtbg_github_updates.offsite_hour", "0")
        self.assertTrue(self.Backup._mtbg_due())
        self._run()
        self.assertFalse(self.Backup._mtbg_due(), "one a day")


@tagged("post_install", "-at_install")
class TestOffsiteWithoutConnector(TransactionCase):

    def test_says_what_is_missing(self):
        with patch.object(type(self.env["ir.config_parameter"]), "get_param", return_value=""):
            if "cloudflare.r2" in self.env:
                with self.assertRaises(UserError):
                    self.env["mtbg.offsite.backup"]._mtbg_r2()
            else:
                with self.assertRaisesRegex(UserError, "Cloudflare Connector"):
                    self.env["mtbg.offsite.backup"]._mtbg_r2()


@tagged("post_install", "-at_install")
class TestOffsiteSchedule(TransactionCase):
    """0.3.0: the fading rules and the frequency are the site's to change."""

    def setUp(self):
        super().setUp()
        self.Rule = self.env["mtbg.offsite.rule"]
        self.Backup = self.env["mtbg.offsite.backup"]

    def test_default_rules_installed(self):
        rules = self.Rule._mtbg_rules()
        self.assertEqual([(n, e, x) for _i, n, e, x in rules], offsite.DEFAULT_RULES)

    def test_custom_rules_fade(self):
        now = datetime.datetime(2028, 11, 10, 2, 30)
        snaps = [(i, now - datetime.timedelta(days=i)) for i in range(0, 200)]
        rules = [(1, "Daily", 1, 14), (2, "Quarterly", 90, 0)]
        kept = offsite.fade(snaps, now, rules)
        self.assertEqual(sum(1 for v in kept.values() if v == "Daily"), 14)
        self.assertTrue(2 <= sum(1 for v in kept.values() if v == "Quarterly") <= 3)
        every = offsite.fade(snaps, now, [(1, "All", 0, 30)])
        self.assertEqual(len(every), 30, "every snapshot for 30 days, then nothing (no forever rule)")

    def test_rule_checks(self):
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.Rule.create({"name": "Too short", "every_days": 1, "expire_days": 3})
        with self.assertRaises(UserError):
            self.Rule.create({"name": "Period too long", "every_days": 40, "expire_days": 40})
        with self.assertRaises(UserError):
            self.Rule.create({"name": "Second forever", "every_days": 365, "expire_days": 0})
        with self.assertRaises(UserError):
            self.Rule.create({"name": "Same expiry", "every_days": 2, "expire_days": 30})
        quarterly = self.Rule.create({"name": "Quarterly", "every_days": 90, "expire_days": 720})
        self.assertIn("one every 90 day(s), until 720 days old", quarterly.summary)

    def test_keep_one_rule_and_reset(self):
        from odoo.exceptions import UserError
        rules = self.Rule.search([])
        rules[1:].unlink()
        with self.assertRaises(UserError):
            rules[0].unlink()
        self.Rule.action_reset_defaults()
        self.assertEqual(len(self.Rule.search([])), len(offsite.DEFAULT_RULES))

    def test_frequency(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("mtbg_github_updates.offsite_enabled", "1")
        ICP.set_param("mtbg_github_updates.offsite_every", "6")
        ICP.set_param("mtbg_github_updates.offsite_unit", "hours")
        self.assertEqual(self.Backup._mtbg_every_hours(), 6)
        self.assertTrue(self.Backup._mtbg_due())
        self.Backup.create({"stamp": "a", "taken_at": datetime.datetime.now() - datetime.timedelta(hours=3),
                            "state": "verified"})
        self.assertFalse(self.Backup._mtbg_due(), "3 hours ago: not yet")
        self.Backup.search([]).write({"taken_at": datetime.datetime.now() - datetime.timedelta(hours=6)})
        self.assertTrue(self.Backup._mtbg_due(), "6 hours ago: due")
        ICP.set_param("mtbg_github_updates.offsite_every", "1")
        ICP.set_param("mtbg_github_updates.offsite_unit", "weeks")
        self.assertEqual(self.Backup._mtbg_every_hours(), 168)
        ICP.set_param("mtbg_github_updates.offsite_hour", "0")
        self.assertFalse(self.Backup._mtbg_due(), "a week has not passed")

    def test_preview(self):
        daily = offsite.simulate(self.Rule._mtbg_rules(), 24)
        hourly = offsite.simulate(self.Rule._mtbg_rules(), 6)
        self.assertTrue(15 <= daily <= 25, daily)
        self.assertLessEqual(abs(daily - hourly), 1, "about the same number whatever the frequency")
        panel = self.env["mtbg.offsite.panel"].create({})
        self.assertIn("snapshots kept", panel.preview)
        self.assertIn("Yearly (kept forever)", panel.rules_text)
        panel.write({"offsite_every": 12, "offsite_unit": "hours"})
        self.assertEqual(self.Backup._mtbg_frequency()[:2], (12, "hours"))

    def test_migration_names_old_codes(self):
        import importlib.util
        b = self.Backup.create({"stamp": "m", "taken_at": datetime.datetime.now(), "state": "verified"})
        self.env.cr.execute("UPDATE mtbg_offsite_backup SET tier = 'weekly' WHERE id = %s", [b.id])
        path = os.path.join(os.path.dirname(offsite.__file__), "..", "migrations", "19.0.0.3.0", "post-migrate.py")
        spec = importlib.util.spec_from_file_location("ghu_030_post", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.migrate(self.env.cr, "19.0.0.2.0")
        b.invalidate_recordset()
        self.assertEqual(b.tier, "Weekly")
