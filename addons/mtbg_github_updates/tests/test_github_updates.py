import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import closing
from unittest.mock import patch

from odoo import sql_db
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import config

from ..models import adopt, store

MANIFEST = "{'name': '%s', 'version': '%s', 'depends': %r, 'installable': True%s}"


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@e"))


def write_module(repo, module, version, depends=("base",), extra="", changelog=None):
    folder = os.path.join(repo, "addons", module)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "__manifest__.py"), "w") as handle:
        handle.write(MANIFEST % (module, version, list(depends), extra))
    open(os.path.join(folder, "__init__.py"), "w").close()
    if changelog:
        with open(os.path.join(folder, "CHANGELOG.md"), "w") as handle:
            handle.write(changelog)


def commit(repo, message, *tags):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    for tag in tags:
        if isinstance(tag, tuple):
            git(repo, "tag", "-a", tag[0], "-m", tag[1])
        else:
            git(repo, "tag", "-a", tag, "-m", message)


@tagged("post_install", "-at_install")
class TestGithubUpdates(TransactionCase):
    """Two throwaway local repositories stand in for GitHub: "tours" (a module in two versions, a package, a
    dependency) and "pack" (a module needing the tours repository's dependency, and one that moved)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="mgu-test-")
        cls.tours = os.path.join(cls.tmp, "tours")
        cls.pack = os.path.join(cls.tmp, "pack")
        for repo in (cls.tours, cls.pack):
            os.makedirs(repo)
            git(repo, "init", "-q")
        declared = ", 'mtbg_github_source': {'repo': %r, 'subdir': 'addons'}"
        write_module(cls.tours, "mgu_demo_a", "19.0.0.1.0")
        commit(cls.tours, "First", "mgu_demo_a-19.0.0.1.0")
        write_module(cls.tours, "mgu_demo_a", "19.0.0.2.0",
                     changelog="# Changelog\n\n## 19.0.0.2.0 · 2026-10-26\n\n- Faster.\n\n## 19.0.0.1.0\n\n- First.\n")
        write_module(cls.tours, "mgu_demo_dep", "19.0.0.1.0")
        commit(cls.tours, "Batch 1", "mgu_demo_a-19.0.0.2.0", "mgu_demo_dep-19.0.0.1.0",
               ("release-tours-20261026-1", "Batch 1: Tours abc\n\nTwo modules."))
        write_module(cls.tours, "mgu_demo_a", "19.0.0.3.0")
        write_module(cls.tours, "mgu_demo_dep", "19.0.0.2.0")
        commit(cls.tours, "Two together, no release tag", "mgu_demo_a-19.0.0.3.0", "mgu_demo_dep-19.0.0.2.0")
        write_module(cls.tours, "mgu_demo_a", "19.0.0.3.9")
        commit(cls.tours, "Wrong number", "mgu_demo_a-19.0.0.4.0")  # its manifest says another version
        # a batch push stacks commits: the release tag on the last one covers every module tag since the last one
        write_module(cls.tours, "mgu_demo_dep", "19.0.0.3.0")
        commit(cls.tours, "Stacked 1", "mgu_demo_dep-19.0.0.3.0")
        write_module(cls.tours, "mgu_demo_a", "19.0.0.5.0")
        commit(cls.tours, "Stacked 2", "mgu_demo_a-19.0.0.5.0", ("release-tours-20261026-2", "Batch 2\n\nStacked."))
        # glues: one completing mgu_demo_a + mgu_demo_dep; one on modules every site has (base, web)
        write_module(cls.tours, "mgu_demo_glue", "19.0.0.1.0", depends=("mgu_demo_a", "mgu_demo_dep"),
                     extra=", 'auto_install': True")
        commit(cls.tours, "Glue", "mgu_demo_glue-19.0.0.1.0")
        write_module(cls.tours, "mgu_demo_glue_web", "19.0.0.1.0", depends=("base", "web"), extra=", 'auto_install': True")
        commit(cls.tours, "Glue on web", "mgu_demo_glue_web-19.0.0.1.0")
        write_module(cls.pack, "mgu_demo_b", "19.0.0.1.0", depends=("base", "mgu_demo_dep"), extra=declared % cls.pack)
        write_module(cls.pack, "mgu_demo_moved", "19.0.0.1.0", extra=declared % cls.tours)
        commit(cls.pack, "Pack", "mgu_demo_b-19.0.0.1.0", "mgu_demo_moved-19.0.0.1.0")
        cls.patcher = patch.dict(config.options, {"mtbg_github_updates_dir": os.path.join(cls.tmp, "updates")})
        cls.patcher.start()
        Source = cls.env["mtbg.github.source"]
        Source.with_context(active_test=False).search([]).unlink()
        cls.src_tours = Source.create({"name": "Tours", "code": "tours", "url": cls.tours, "subdir": "addons",
                                       "module_patterns": "mgu_demo_a,mgu_demo_dep,mgu_demo_moved,mgu_demo_glue*"})
        cls.src_pack = Source.create({"name": "Pack", "code": "pack", "url": cls.pack, "subdir": "addons"})
        cls.src_tours._mtbg_refresh()
        cls.src_pack._mtbg_refresh()
        cls.Update = cls.env["mtbg.github.update"].with_context(mtbg_no_restart=True)

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def release(self, source, tag):
        return source.release_ids.filtered(lambda r: r.tag == tag)

    def ready(self):
        """The store first in addons_path, no other copy, a backup that is only a name."""
        backup = os.path.join(store.root(), "backups", "fake.dump")
        return [patch.object(store, "in_addons_path", return_value=True),
                patch.object(store, "shadowed_by", return_value=None),
                patch.object(store, "backup_database", return_value=(backup, ""))]

    def start(self, plan, **kw):
        patches = self.ready()
        for p in patches:
            p.start()
        try:
            return self.Update._mtbg_start(plan, **kw)
        finally:
            for p in patches:
                p.stop()

    def tearDown(self):
        for module in ("mgu_demo_a", "mgu_demo_dep", "mgu_demo_b"):
            store.switch(module, None)
        super().tearDown()

    # ------------------------------------------------------------------ releases and packages
    def test_releases_and_packages(self):
        tags = sorted(self.src_tours.release_ids.mapped("tag"))
        self.assertIn("mgu_demo_a-19.0.0.2.0", tags)
        a2 = self.release(self.src_tours, "mgu_demo_a-19.0.0.2.0")
        self.assertEqual(a2.changelog, "- Faster.", "its CHANGELOG section")
        packages = self.src_tours.package_ids
        first = packages.filtered(lambda p: p.tag == "release-tours-20261026-1")
        self.assertEqual(first.name, "Batch 1: Tours abc")
        self.assertEqual(first.notes, "Two modules.")
        self.assertEqual(sorted(first.release_ids.mapped("tag")), ["mgu_demo_a-19.0.0.2.0", "mgu_demo_dep-19.0.0.1.0"],
                         "the first release tag: the tags on its own commit")
        second = packages.filtered(lambda p: p.tag == "release-tours-20261026-2")
        self.assertEqual(sorted(second.release_ids.mapped("tag")),
                         ["mgu_demo_a-19.0.0.3.0", "mgu_demo_a-19.0.0.4.0", "mgu_demo_a-19.0.0.5.0",
                          "mgu_demo_dep-19.0.0.2.0", "mgu_demo_dep-19.0.0.3.0"],
                         "every module tag after the previous release tag, on several commits")
        self.assertEqual(len(packages), 2, "a commit with two tags inside a release's range is not a package of its own")
        self.assertNotIn("mgu_demo_a-19.0.0.1.0", packages.release_ids.mapped("tag"), "one tag alone: no package")

    def test_module_offered_only_from_its_own_repository(self):
        """mgu_demo_moved is tagged in the pack repository, but its manifest names the tours repository."""
        self.assertEqual(self.src_pack.release_ids.mapped("module"), ["mgu_demo_b"])

    def test_tag_must_match_the_manifest(self):
        with self.assertRaisesRegex(store.StoreError, "says version"):
            store.install_version("tours", self.tours, "addons", "mgu_demo_a", "19.0.0.4.0", "mgu_demo_a-19.0.0.4.0")
        self.assertFalse(os.path.exists(os.path.join(store.version_dir("mgu_demo_a", "19.0.0.4.0"), "mgu_demo_a")))

    def test_new_dependency_from_another_repository(self):
        b = self.release(self.src_pack, "mgu_demo_b-19.0.0.1.0")
        required = self.Update._mtbg_new_dependencies([("mgu_demo_b", b)])
        self.assertEqual([(m, r.tag, by) for m, r, by in required],
                         [("mgu_demo_dep", "mgu_demo_dep-19.0.0.3.0", "mgu_demo_b")])

    # ------------------------------------------------------------------ a batch
    def test_batch_switch_then_finish_after_restart(self):
        b = self.release(self.src_pack, "mgu_demo_b-19.0.0.1.0")
        action = self.start([("mgu_demo_b", b)], kind="install")
        self.assertEqual(action["tag"], "mtbg_github_progress")
        name = action["params"]["batch"]
        header = self.env["mtbg.github.batch"].search([("name", "=", name)])
        self.assertEqual(header.kind, "install")
        self.assertTrue(header.backup_path and header.backup_at)
        lines = self.Update.search([("batch", "=", name)], order="id")
        self.assertEqual([(l.module, l.kind) for l in lines], [("mgu_demo_b", "install"), ("mgu_demo_dep", "install")])
        self.assertEqual(store.active_version("mgu_demo_dep"), "19.0.0.3.0", "the new dependency's code is in the store")
        self.assertEqual(self.Update.mtbg_apply(name), "waiting", "the same process waits for the restart")
        self.assertEqual(self.Update.mtbg_progress(None)[0]["state"], "restarting", "a reloaded page finds it")
        with self.assertRaisesRegex(UserError, "still running"):
            self.start([("mgu_demo_b", b)])

    def test_shadowed_module_refused(self):
        a2 = self.release(self.src_tours, "mgu_demo_a-19.0.0.2.0")
        with patch.object(store, "in_addons_path", return_value=True), \
                patch.object(store, "shadowed_by", return_value="/opt/other-tree"), \
                patch.object(store, "backup_database", return_value=("/x", "")):
            with self.assertRaisesRegex(UserError, "/opt/other-tree"):
                self.Update._mtbg_start([("mgu_demo_a", a2)])
        self.assertIsNone(store.active_version("mgu_demo_a"), "nothing switched")

    def test_store_must_come_first(self):
        with patch.object(store, "configured_addons_paths", return_value=["/somewhere", store.active_dir()]):
            self.assertFalse(store.in_addons_path())
        with patch.object(store, "configured_addons_paths", return_value=[os.path.realpath(store.active_dir())]):
            self.assertTrue(store.in_addons_path())

    def test_page_and_plan(self):
        page = self.env["mtbg.github.source"].mtbg_updates_page()
        self.assertEqual([s["name"] for s in page["sections"]][:2], ["Tours", "Pack"])
        tours = page["sections"][0]
        self.assertEqual(len(tours["packages"]), 2)
        self.assertTrue(all(row["what"] == "not here" for row in tours["packages"][0]["modules"]))
        self.assertIn("mgu_demo_a", [a["name"] for a in tours["available"]])
        b = self.release(self.src_pack, "mgu_demo_b-19.0.0.1.0")
        plan = self.env["mtbg.github.source"].mtbg_plan("install", [b.id])
        self.assertTrue(plan["ok"], plan)
        self.assertEqual([r["module"] for r in plan["rows"]], ["mgu_demo_b", "mgu_demo_dep"])

    def test_upgrade_in_its_own_process(self):
        with patch.object(store.subprocess, "run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "ok", ""
            self.assertEqual(store.upgrade_database("some_db", ["mgu_demo_a"], install=["mgu_demo_b"]), (True, ""))
        command = run.call_args.args[0]
        self.assertEqual(command[-11:-1], ["-d", "some_db", "-i", "mgu_demo_b", "-u", "mgu_demo_a",
                                           "--stop-after-init", "--no-http", "--max-cron-threads=0", "--log-level=info"])
        with self.assertRaises(store.StoreError):
            store.upgrade_database("some_db", ["x; rm -rf /"])

    def test_token_through_the_environment(self):
        store.save_token("tok", "github_pat_abcdefghijkl")
        self.assertEqual(oct(os.stat(store.token_path("tok")).st_mode & 0o777), "0o600")
        env = store.git_env("tok", "https://github.com/o/r.git")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertNotIn("github_pat", env["GIT_CONFIG_VALUE_0"], "base64, never the token as typed")
        with self.assertRaises(store.StoreError):
            store.save_token("tok", "bad token; rm")
        store.save_token("tok", "")
        self.assertFalse(os.path.exists(store.token_path("tok")))

    # ------------------------------------------------------------------ going back
    def test_code_only_go_back_only_when_safe(self):
        folder = os.path.join(self.tmp, "mod")
        os.makedirs(os.path.join(folder, "migrations", "19.0.0.2.0"))
        self.assertTrue(store.has_migrations(folder, "19.0.0.1.0", "19.0.0.2.0"))
        self.assertFalse(store.has_migrations(folder, "19.0.0.2.0", "19.0.0.3.0"))
        os.makedirs(os.path.join(folder, "migrations", "0.3.0"))
        self.assertTrue(store.has_migrations(folder, "19.0.0.2.0", "19.0.0.3.0"), "short form: 0.3.0")
        self.assertFalse(self.Update.mtbg_revert_preview("mgu_demo_a")["ok"], "never updated from here")

    def test_rollback_preview_scope_and_confirmation(self):
        Batch = self.env["mtbg.github.batch"]
        dump = os.path.join(store.root(), "backups", "db.dump")
        os.makedirs(os.path.dirname(dump), exist_ok=True)
        open(dump, "w").close()
        first = Batch.create({"name": "b1", "kind": "update", "backup_path": dump,
                              "backup_at": "2026-10-26 10:00:00", "title": "First"})
        self.Update.create({"batch": "b1", "batch_id": first.id, "module": "mgu_demo_a", "kind": "update",
                            "from_version": "19.0.0.1.0", "to_version": "19.0.0.2.0", "state": "applied",
                            "previous_target": "/store/a/1"})
        second = Batch.create({"name": "b2", "kind": "install", "backup_path": dump, "backup_at": "2026-10-26 11:00:00"})
        self.Update.create({"batch": "b2", "batch_id": second.id, "module": "mgu_demo_a", "kind": "update",
                            "from_version": "19.0.0.2.0", "to_version": "19.0.0.3.0", "state": "applied"})
        self.Update.create({"batch": "b2", "batch_id": second.id, "module": "mgu_demo_b", "kind": "install",
                            "to_version": "19.0.0.1.0", "state": "applied"})
        # someone's data since the backup (the superuser's writes, the upgrade's own, are not counted)
        self.env["res.partner"].with_user(self.env.ref("base.user_admin")).create({"name": "Entered after the backup"})
        with patch.object(store, "database_rights", return_value=(True, "")):
            preview = first.mtbg_rollback_preview()
        self.assertEqual(preview["problems"], [])
        self.assertEqual([u["name"] for u in preview["updates"]], ["b1", "b2"], "the later update is undone too")
        modules = {m["module"]: m for m in preview["modules"]}
        self.assertEqual(modules["mgu_demo_a"]["back_to"], "19.0.0.1.0", "back to before the FIRST one")
        self.assertTrue(modules["mgu_demo_b"]["removed"])
        self.assertTrue(any(d["count"] for d in preview["data"]), "the data entered since is counted")
        with patch.object(store, "database_rights", return_value=(True, "")):
            with self.assertRaisesRegex(UserError, "Type the database"):
                first.mtbg_rollback("not-this-one")
            result = first.with_context(mtbg_no_restart=True).mtbg_rollback(self.env.cr.dbname)
        plan = result["plan"]
        self.assertEqual(plan["links"], {"mgu_demo_a": "/store/a/1", "mgu_demo_b": None})
        self.assertEqual(plan["undone"], ["b1", "b2"])
        with open(result["journal"]) as handle:
            self.assertEqual(json.load(handle)["state"], "started")
        restore = Batch.search([("kind", "=", "restore")], limit=1)
        self.assertEqual(restore.restore_state, "restoring")
        self.assertIn("A rollback", self.Update._mtbg_busy_elsewhere(), "nothing else starts meanwhile")
        with patch.object(store, "database_rights", return_value=(False, "no CREATEDB")):
            self.assertTrue(second.mtbg_rollback_preview()["problems"])

    def test_restore_and_swap_real_databases(self):
        """The restore's database steps, on throwaway databases: dump, change, restore into a new one, swap."""
        name, restored, kept = "mgu_t_%s" % os.getpid(), "mgu_t_%s_r" % os.getpid(), "mgu_t_%s_k" % os.getpid()

        def sql(db, query):
            with closing(sql_db.db_connect(db).cursor()) as cr:
                cr.execute(query)
                rows = cr.fetchall() if cr.description else None
                cr.commit()
                return rows
        for db in (name, restored, kept):
            store.drop_database(db)
        subprocess.run(["createdb", name], check=True, capture_output=True)
        try:
            sql(name, "CREATE TABLE t (x int); INSERT INTO t VALUES (1)")
            dump, snapshot = store.backup_database(name, "test")
            self.assertEqual(snapshot, "", "no filestore: no snapshot")
            sql(name, "INSERT INTO t VALUES (2)")
            self.assertEqual(store.database_rights(name), (True, ""))
            store.restore_database(dump, restored)
            sql_db.close_db(name)
            store.swap_databases(name, restored, kept)
            self.assertEqual(sql(name, "SELECT count(*) FROM t")[0][0], 1, "the site's name: the backup")
            self.assertEqual(sql(kept, "SELECT count(*) FROM t")[0][0], 2, "the database before, kept")
            with self.assertRaises(store.StoreError):
                store.restore_database("/etc/passwd", restored)
        finally:
            for db in (name, restored, kept):
                sql_db.close_db(db)
                store.drop_database(db)

    def test_filestore_snapshot_and_swap(self):
        data = os.path.join(self.tmp, "data")
        with patch.dict(config.options, {"data_dir": data}):
            files = store.filestore_dir("mgu_fs")
            os.makedirs(os.path.join(files, "ab"))
            with open(os.path.join(files, "ab", "f1"), "w") as handle:
                handle.write("one")
            def dump(tool, *args, **kw):
                open(args[args.index("--file") + 1], "w").close()  # pg_dump's file
            with patch.object(store, "_pg", side_effect=dump):
                _dump, snapshot = store.backup_database("mgu_fs", "x")
            self.assertTrue(os.path.isfile(os.path.join(snapshot, "ab", "f1")))
            with open(os.path.join(files, "ab", "f2"), "w") as handle:
                handle.write("two, after the backup")
            store.swap_filestore("mgu_fs", snapshot, "mgu_fs_kept")
            self.assertEqual(sorted(os.listdir(os.path.join(files, "ab"))), ["f1"])
            self.assertTrue(os.path.isfile(os.path.join(store.filestore_dir("mgu_fs_kept"), "ab", "f2")))

    def test_retention(self):
        Batch = self.env["mtbg.github.batch"]
        folder = store.path("backups", "ret")
        os.makedirs(folder, exist_ok=True)
        batches = Batch
        for i in range(13):
            dump = os.path.join(folder, "%02d.dump" % i)
            open(dump, "w").close()
            batches |= Batch.create({"name": "ret%02d" % i, "kind": "update", "backup_path": dump})
        self.env.cr.execute("UPDATE mtbg_github_batch SET create_date = now() - interval '40 days' WHERE id IN %s",
                            (tuple(batches.ids),))
        batches.invalidate_recordset()
        self.Update.create({"batch": "ret00", "batch_id": batches[0].id, "module": "mgu_only_once", "kind": "update",
                            "state": "applied"})
        Batch._cron_retention()
        purged = batches.filtered("backup_purged")
        self.assertEqual(len(purged), 2, "13 batches: the newest 10 and the last one of each module stay")
        self.assertFalse(batches[0].backup_purged, "the newest backup before mgu_only_once's update")
        for batch in purged:
            self.assertFalse(os.path.exists(batch.backup_path))

    # ------------------------------------------------------------------ taking over
    def test_take_over_an_earlier_store(self):
        old = os.path.join(self.tmp, "old_tours")
        target = os.path.join(old, "store", "versions", "mgu_demo_a", "19.0.0.2.0", "mgu_demo_a")
        os.makedirs(target)
        with open(os.path.join(target, "__manifest__.py"), "w") as handle:
            handle.write(MANIFEST % ("mgu_demo_a", "19.0.0.2.0", ["base"], ""))
        os.makedirs(os.path.join(old, "store", "active"))
        os.symlink(target, os.path.join(old, "store", "active", "mgu_demo_a"))
        empty = os.path.join(self.tmp, "old_cfd")
        Batch = self.env["mtbg.github.batch"].with_context(mtbg_no_restart=True)
        with patch.dict(config.options, {"mtbg_updates_dir": old, "cfd_updates_dir": empty}), \
                patch.object(store, "in_addons_path", return_value=True), \
                patch.object(store, "shadowed_by", return_value=None), \
                patch.object(type(self.Update), "_mtbg_trigger_and_restart"):
            preview = Batch.mtbg_adopt_preview()
            self.assertEqual(preview["todo"], 1)
            action = Batch.mtbg_adopt()
            self.assertEqual(store.active_version("mgu_demo_a"), "19.0.0.2.0")
            self.assertTrue(store.active_target("mgu_demo_a").startswith(os.path.realpath(store.root())),
                            "a copy in this store, not a link to the old one")
            self.assertTrue(os.path.islink(os.path.join(old, "store", "active", "mgu_demo_a")), "the old store untouched")
            lines = self.Update.search([("batch", "=", action["params"]["batch"])])
            self.assertEqual((lines.kind, lines.from_version, lines.to_version), ("adopt", "19.0.0.2.0", "19.0.0.2.0"))
            self.assertEqual(Batch.mtbg_adopt_preview()["todo"], 0, "once")
            # updated here since: the old store's older copy is never taken again
            newer = os.path.join(store.version_dir("mgu_demo_a", "19.0.0.3.0"), "mgu_demo_a")
            shutil.copytree(store.active_target("mgu_demo_a"), newer)
            store.switch("mgu_demo_a", newer)
            self.assertEqual(Batch.mtbg_adopt_preview()["todo"], 0)
            lines.write({"state": "applied"})  # (the first take-over finished after its restart)
            Batch.mtbg_adopt()
            self.assertEqual(store.active_version("mgu_demo_a"), "19.0.0.3.0", "not taken back to the old store's")
        self.assertEqual(adopt.old_links(empty), {})

    def test_take_over_leaves_work_in_progress_alone(self):
        """A module loaded from another folder (a dev site's work in progress, after the old store in addons_path) is
        not copied: with this store first, the copy would hide it."""
        old = os.path.join(self.tmp, "old_wip")
        for module in ("mgu_demo_a", "mgu_demo_dep"):
            target = os.path.join(old, "store", "versions", module, "19.0.0.1.0", module)
            os.makedirs(target)
            with open(os.path.join(target, "__manifest__.py"), "w") as handle:
                handle.write(MANIFEST % (module, "19.0.0.1.0", ["base"], ""))
            os.makedirs(os.path.join(old, "store", "active"), exist_ok=True)
            os.symlink(target, os.path.join(old, "store", "active", module))
        active = os.path.realpath(os.path.join(old, "store", "active"))
        Batch = self.env["mtbg.github.batch"].with_context(mtbg_no_restart=True)
        loaded = {"mgu_demo_a": "/srv/dev-addons", "mgu_demo_dep": active}
        with patch.dict(config.options, {"mtbg_updates_dir": old, "cfd_updates_dir": os.path.join(self.tmp, "none")}), \
                patch.object(store, "in_addons_path", return_value=True), \
                patch.object(store, "shadowed_by", return_value=None), \
                patch.object(adopt, "loaded_folder", side_effect=lambda m: loaded.get(m, "")), \
                patch.object(type(self.Update), "_mtbg_trigger_and_restart"):
            page = self.env["mtbg.github.source"].mtbg_updates_page()
            self.assertEqual([l["module"] for l in page["adopt"]["left"]], ["mgu_demo_a"])
            self.assertIn("/srv/dev-addons", page["adopt"]["left"][0]["why"])
            Batch.mtbg_adopt()
        self.assertIsNone(store.active_version("mgu_demo_a"), "the work in progress stays the one loaded")
        self.assertEqual(store.active_version("mgu_demo_dep"), "19.0.0.1.0")

    def test_restarted_batches_finished_by_the_job_alone(self):
        """No progress screen open: the scheduled job of the restarted server finishes a take-over (with a linked module
        this database does not have installed) and an upgrade the process of its own made."""
        module = self.env.ref("base.module_mtbg_github_updates")
        Batch = self.env["mtbg.github.batch"]
        adopt = Batch.create({"name": "fin-adopt", "kind": "adopt"})
        a1 = self.Update.create({"batch": "fin-adopt", "batch_id": adopt.id, "module": module.name, "kind": "adopt",
                                 "from_version": "19.0.0.0.1", "to_version": "19.0.0.0.1", "switched_at": 1})
        a2 = self.Update.create({"batch": "fin-adopt", "batch_id": adopt.id, "module": "mtbg_tours_customer360",
                                 "kind": "adopt", "to_version": "19.0.0.3.2", "switched_at": 1})
        upd = Batch.create({"name": "fin-upd", "kind": "update"})
        u1 = self.Update.create({"batch": "fin-upd", "batch_id": upd.id, "module": module.name, "kind": "update",
                                 "to_version": module.latest_version, "switched_at": 1})
        later = self.Update.create({"batch": "fin-later", "module": module.name, "kind": "update",
                                    "to_version": module.latest_version, "switched_at": time.time() + 3600})
        with patch.object(type(module), "button_immediate_upgrade", side_effect=AssertionError("no module operation")):
            self.Update._cron_finish_restarted()
        self.assertEqual((a1.state, a2.state, u1.state), ("applied", "applied", "applied"))
        self.assertEqual(later.state, "restarting", "switched after this process started: not restarted yet")
        later.switched_at = 1
        with patch.object(type(module), "button_immediate_upgrade", side_effect=AssertionError("no module operation")):
            self.env["mtbg.github.source"].mtbg_updates_page()
        self.assertEqual(later.state, "applied", "opening the Updates page finishes it too")

    def test_glue_only_with_the_batch_it_completes(self):
        a5 = self.release(self.src_tours, "mgu_demo_a-19.0.0.5.0")
        dep3 = self.release(self.src_tours, "mgu_demo_dep-19.0.0.3.0")
        glues = lambda plan: [m for m, _r, _w in self.Update._mtbg_auto_installs(plan, [])]
        self.assertEqual(glues([("mgu_demo_a", a5), ("mgu_demo_dep", dep3)]), ["mgu_demo_glue"])
        self.assertEqual(glues([("mgu_demo_a", a5)]), [], "mgu_demo_dep neither installed nor in the batch")
        b = self.release(self.src_pack, "mgu_demo_b-19.0.0.1.0")
        self.assertNotIn("mgu_demo_glue_web", glues([("mgu_demo_b", b)]),
                         "a glue whose modules are all installed does not join a batch that touches none of them")

    def test_older_releases_never_offered_or_installed(self):
        """A site loading a module from outside the store (a dev site, a bundled tree) at a newer version than an
        old package: the package says up to date, and a batch with an older version is refused before anything."""
        module = self.env.ref("base.module_mtbg_github_updates")
        current = module._mgu_current()
        self.assertTrue(current)
        old = self.env["mtbg.github.release"].create({"source_id": self.src_tours.id, "module": module.name,
                                                       "version": "19.0.0.0.1", "tag": module.name + "-19.0.0.0.1"})
        new = self.env["mtbg.github.release"].create({"source_id": self.src_tours.id, "module": module.name,
                                                       "version": "19.0.9.0.0", "tag": module.name + "-19.0.9.0.0"})
        package = self.env["mtbg.github.package"].create({"source_id": self.src_tours.id, "commit": "f" * 40,
                                                          "name": "Old", "release_ids": [(6, 0, old.ids)]})
        self.assertEqual([what for _m, _r, what in package._mtbg_plan()], ["same"])
        package.release_ids = [(6, 0, (old | new).ids)]
        self.assertEqual([(r.version, what) for _m, r, what in package._mtbg_plan()], [("19.0.9.0.0", "update")],
                         "the newest of a module in a package")
        with patch.object(store, "in_addons_path", return_value=True), \
                patch.object(store, "backup_database", return_value=("/x", "")):
            with self.assertRaisesRegex(UserError, "not newer than what this site has"):
                self.Update._mtbg_start([(module.name, old)])
        self.assertIsNone(store.active_version(module.name), "nothing switched")

    def test_known_repositories_offered(self):
        Source = self.env["mtbg.github.source"]
        keys = [k["key"] for k in Source._mtbg_known_missing()]
        self.assertIn("blocks", keys)
        source = Source.browse(Source.mtbg_add_known("blocks"))
        self.assertEqual((source.subdir, source.url.endswith("mtbg-website-blocks.git")), ("addons", True))
        self.assertNotIn("blocks", [k["key"] for k in Source._mtbg_known_missing()], "once")
        with self.assertRaises(UserError):
            Source.mtbg_add_known("blocks")
        known = [k["name"] for k in Source.mtbg_updates_page()["known"]]
        self.assertNotIn("Website Blocks", known)
        self.assertIn("MTBG Tours", known, "the others still offered")
        self.assertIn("does not read yet", Source._mtbg_known_hint())

    def test_moved_module_newest_from_any_source(self):
        """A module released from its new repository: its newest there, though the installed manifest is the old
        repository's (no key) and the old one's patterns still match it."""
        module = self.env.ref("base.module_mtbg_github_updates")
        Release = self.env["mtbg.github.release"]
        Release.create({"source_id": self.src_tours.id, "module": module.name, "version": "19.0.5.0.0",
                        "tag": module.name + "-19.0.5.0.0"})
        moved = Release.create({"source_id": self.src_pack.id, "module": module.name, "version": "19.0.6.0.0",
                                "tag": module.name + "-19.0.6.0.0"})
        module.invalidate_recordset()
        self.assertEqual((module.mgu_newest_release_id, module.mgu_source_id), (moved, self.src_pack))
        self.assertTrue(module.mgu_update)

    def test_released_together_installed_together(self):
        """Batch 7's split (DEV2, DEV3): the pack's module pulled a glue from the Tours release, whose sibling (the
        catalog the glue's model came from) must come too. Here: mgu_demo_b (pack) needs mgu_demo_dep (tours), whose
        release is in package 2 with mgu_demo_a, which this site has older: it comes, and then the glue
        completing a + dep."""
        self.env["ir.module.module"].create({"name": "mgu_demo_a", "state": "installed",
                                             "latest_version": "19.0.0.1.0", "shortdesc": "Demo A"})
        b = self.release(self.src_pack, "mgu_demo_b-19.0.0.1.0")
        everything = {m: (r.version if r else "", why) for m, r, why in self.Update._mtbg_full_plan([("mgu_demo_b", b)])}
        self.assertEqual(set(everything), {"mgu_demo_b", "mgu_demo_dep", "mgu_demo_a", "mgu_demo_glue"})
        self.assertEqual(everything["mgu_demo_a"][0], "19.0.0.5.0", "the package's newest")
        self.assertIn("released with", everything["mgu_demo_a"][1])
        plan = self.env["mtbg.github.source"].mtbg_plan("install", [b.id])
        self.assertEqual({r["module"] for r in plan["rows"]}, set(everything), "the dialog shows the same")

    def test_failed_upgrade_brings_the_backup_back(self):
        """A failed upgrade may have migrated the database already: the batch's backup (database, files, code)
        comes back, not the old code alone over the changed database."""
        Batch = self.env["mtbg.github.batch"]
        dump = os.path.join(store.root(), "backups", "fail.dump")
        os.makedirs(os.path.dirname(dump), exist_ok=True)
        open(dump, "w").close()
        header = Batch.create({"name": "failb", "kind": "package", "backup_path": dump, "title": "Batch 7"})
        line = self.Update.create({"batch": "failb", "batch_id": header.id, "module": "mgu_demo_a", "kind": "update",
                                   "from_version": "19.0.0.1.0", "to_version": "19.0.0.2.0",
                                   "previous_target": "/store/a/1", "switched_at": 1})
        new = self.Update.create({"batch": "failb", "batch_id": header.id, "module": "mgu_demo_b", "kind": "install",
                                  "to_version": "19.0.0.1.0", "switched_at": 1})
        plan, journal = (line | new)._mtbg_prepare_restore("KeyError 'tag_ids'")
        self.assertEqual(plan["dump"], dump)
        self.assertEqual(plan["links"], {"mgu_demo_a": "/store/a/1", "mgu_demo_b": None})
        self.assertTrue(os.path.isfile(journal))
        self.assertEqual((line.state, new.state), ("failed", "failed"))
        self.assertIn("database, the files and the code are back", line.message)
        self.assertEqual([row["batch"]["kind"] for row in plan["history"]], ["package", "restore"])
        header.backup_path = False
        self.assertIsNone((line | new)._mtbg_prepare_restore("x"), "no backup: the code alone goes back")

    def test_store_of_one_database(self):
        self.assertEqual(store.claim(self.env.cr.dbname, "u1"), "", "the first database to use it")
        self.assertEqual(store.claim(self.env.cr.dbname, "u1"), "")
        problem = store.claim("another_site", "u2")
        self.assertIn("belongs to the database %s" % self.env.cr.dbname, problem)
        with patch.object(store, "claim", return_value=problem), \
                patch.object(store, "in_addons_path", return_value=True):
            with self.assertRaisesRegex(UserError, "belongs to the database"):
                self.Update._mtbg_start([("mgu_demo_a", self.release(self.src_tours, "mgu_demo_a-19.0.0.2.0"))])
            self.assertTrue(self.env["mtbg.github.source"].mtbg_updates_page()["owner_problem"])
