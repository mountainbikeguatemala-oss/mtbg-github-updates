"""The extensions board (0.4.0): its stored values (source, status, versions, folder), its links across
repositories, its actions; and the board, the list and the form in a browser."""
import os
from unittest.mock import patch

from odoo.tests import HttpCase, TransactionCase, tagged

from ..models import board, store


def _board_case(case):
    env = case.env
    Source = env["mtbg.github.source"]
    Release = env["mtbg.github.release"]
    Source.with_context(active_test=False).search([]).unlink()
    case.src_a = Source.create({"name": "Repo A", "code": "board_a", "url": "git@github.com:o/repo-a.git",
                                "subdir": "addons", "sequence": 1})
    case.src_b = Source.create({"name": "Repo B", "code": "board_b", "url": "git@github.com:o/repo-b.git",
                                "subdir": "", "sequence": 2})
    case.updater = env.ref("base.module_mtbg_github_updates")
    case.web = env.ref("base.module_web")
    case.new = env["ir.module.module"].search([("state", "=", "uninstalled"), ("name", "!=", "mtbg_github_updates")],
                                              limit=1)
    running = case.updater.mgu_running_version
    # the updater: a newer release on repo A; a module not installed: released on repo B
    Release.create({"source_id": case.src_a.id, "module": case.updater.name, "version": "19.0.99.0.0",
                    "tag": case.updater.name + "-19.0.99.0.0"})
    Release.create({"source_id": case.src_a.id, "module": case.updater.name, "version": running,
                    "tag": "%s-%s" % (case.updater.name, running)})
    Release.create({"source_id": case.src_b.id, "module": case.new.name, "version": "19.0.1.0.0",
                    "tag": case.new.name + "-19.0.1.0.0"})


@tagged("post_install", "-at_install")
class TestExtensionsBoard(TransactionCase):

    def setUp(self):
        super().setUp()
        _board_case(self)
        self.Module = self.env["ir.module.module"]

    def test_statuses_versions_and_sources(self):
        self.Module._mgu_board_refresh()
        self.assertTrue(self.updater.mgu_board)
        self.assertEqual((self.updater.mgu_board_source_id, self.updater.mgu_status), (self.src_a, "update"))
        self.assertEqual(self.updater.mgu_board_available, "19.0.99.0.0")
        self.assertTrue(self.updater.mgu_board_running)
        self.assertEqual((self.new.mgu_board, self.new.mgu_status, self.new.mgu_board_source_id),
                         (True, "new", self.src_b), "released, not installed: new")
        self.assertFalse(self.new.mgu_board_running)
        self.assertEqual((self.web.mgu_board, self.web.mgu_status, self.web.mgu_board_source_id.id),
                         (True, "local", False), "installed, from no source")
        other = self.Module.search([("state", "=", "uninstalled"), ("id", "!=", self.new.id)], limit=1)
        self.assertFalse(other.mgu_board, "neither installed nor released: not on the board")
        # up to date once the newest is the running one; an extension leaving the sources leaves its column
        self.env["mtbg.github.release"].search([("version", "=", "19.0.99.0.0")]).unlink()
        self.src_b.unlink()
        self.Module.invalidate_model()
        self.Module._mgu_board_refresh()
        self.assertEqual(self.updater.mgu_status, "current")
        self.assertFalse(self.new.mgu_board)

    def test_error_after_a_failed_update(self):
        self.env["mtbg.github.update"].create({"module": self.updater.name, "state": "failed", "kind": "update",
                                               "batch": "test"})
        self.Module._mgu_board_refresh()
        self.assertEqual(self.updater.mgu_status, "error")

    def test_folders(self):
        roots = board._odoo_roots()
        kind, folder, shadowed = self.web._mgu_folder_of(roots)
        self.assertEqual(kind, "odoo")
        self.assertTrue(os.path.isdir(folder))
        with patch.object(store, "active_dir", return_value=os.path.dirname(
                os.path.dirname(os.path.realpath(board.__file__))) + "/.."):
            self.assertEqual(self.updater._mgu_folder_of(roots)[0], "store", "loaded from the store's folder")
        self.assertEqual(self.updater._mgu_folder_of(roots)[0], "addons", "a folder of the addons path")
        self.Module._mgu_board_refresh()
        self.assertFalse(self.updater.mgu_shadowed, "the store holds no version of it: nothing hidden")
        with patch.object(store, "active_version", return_value="19.0.99.0.0"):
            self.Module._mgu_board_refresh()
        self.assertEqual(self.updater.mgu_shadowed, folder_of_updater := self.updater._mgu_folder_of(roots)[1],
                         "the store's version is hidden behind the copy Odoo loads")
        self.assertTrue(folder_of_updater)

    def test_needs_and_used_by_across_repositories(self):
        self.assertIn(self.env.ref("base.module_base"), self.updater.mgu_needs_ids)
        self.assertIn(self.env.ref("base.module_web"), self.updater.mgu_needs_ids)
        self.assertIn(self.updater, self.env.ref("base.module_web").mgu_used_by_ids)
        self.assertEqual(set(self.updater.mgu_release_ids.mapped("version")) >= {"19.0.99.0.0"}, True)

    def test_actions(self):
        action = self.Module.action_mgu_board()
        self.assertEqual((action["res_model"], action["domain"]), ("ir.module.module", "[('mgu_board', '=', True)]"))
        self.assertTrue(self.updater.mgu_board, "refreshed on opening")
        one = self.updater.action_mgu_open_one()
        self.assertEqual((one["tag"], one["params"]["modules"]), ("mtbg_github_updates", [self.updater.name]))
        github = self.updater.action_mgu_open_github()
        self.assertEqual(github["url"], "https://github.com/o/repo-a/tree/%s-19.0.99.0.0/addons/%s" % (
            self.updater.name, self.updater.name))
        access = self.src_a.action_mgu_access()
        self.assertEqual((access["res_model"], access["res_id"], access["target"]), ("mtbg.github.source",
                                                                                     self.src_a.id, "new"))

    def test_check_github_refreshes_the_board(self):
        with patch.object(type(self.src_a), "_mtbg_refresh", autospec=True), \
                patch.object(type(self.src_a), "_mtbg_sync_declared", autospec=True):
            self.src_a.action_check_updates()
        self.assertTrue(self.updater.mgu_board)

    def test_administrators_only(self):
        user = self.env["res.users"].create({"name": "Board user", "login": "mgu_board_user",
                                             "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.Module.with_user(user).action_mgu_board()


@tagged("post_install", "-at_install")
class TestExtensionsBoardInBrowser(HttpCase):
    """The board (a column per source, its header's actions, the cards), the list and the form, in a browser."""

    def setUp(self):
        super().setUp()
        _board_case(self)
        self.env["ir.module.module"]._mgu_board_refresh()
        self.action = self.env.ref("mtbg_github_updates.action_mgu_extensions")

    def _wait(self, code):
        return """
const wait = (test, what) => new Promise((ok, ko) => { let n = 0; const t = setInterval(() => {
    const v = test(); if (v) { clearInterval(t); ok(v); } else if (++n > 150) { clearInterval(t); ko(new Error(what)); } }, 100); });
(async () => {
%s
    console.log('test successful');
})().catch((e) => console.error('test failed: ' + e.message));
""" % code

    def test_board(self):
        code = self._wait("""
    await wait(() => document.querySelector('.o_mgu_board .o_kanban_group'), 'no column');
    const titles = [...document.querySelectorAll('.o_kanban_group .o_column_title')].map((e) => e.textContent);
    if (!titles.some((t) => t.includes('Repo A'))) throw new Error('no Repo A column: ' + titles);
    if (!document.querySelector('.o_kanban_group .o_mgu_source_tools .o_mgu_check')) throw new Error('no Check GitHub');
    if (!document.querySelector('.o_kanban_group .o_mgu_source_tools .o_mgu_access')) throw new Error('no Access');
    const card = [...document.querySelectorAll('.o_kanban_record')].find((c) => c.textContent.includes('mtbg_github_updates'));
    if (!card) throw new Error('no card for the updater');
    if (!card.textContent.includes('Update')) throw new Error('the card has no Update status');
    if (!card.textContent.includes('19.0.99.0.0')) throw new Error('the card has no GitHub version');
    if (!document.querySelector('.o_search_panel')) throw new Error('no side filter');
    document.querySelector('.o_kanban_group .o_mgu_access').click();
    await wait(() => document.querySelector('.modal .o_form_view'), 'Access opened no form');
""")
        self.browser_js("/odoo/action-%d" % self.action.id, code, login="admin")

    def test_list_and_form(self):
        code = self._wait("""
    await wait(() => document.querySelector('.o_mgu_board'), 'no board');
    document.querySelector('.o_switch_view.o_list').click();
    await wait(() => document.querySelector('.o_mgu_list'), 'no list');
    await wait(() => document.querySelector('.o_mgu_list .o_group_header'), 'the list is not grouped by source');
    document.querySelector('.o_mgu_list .o_group_header').click();
    const row = await wait(() => [...document.querySelectorAll('.o_mgu_list .o_data_row')].find(
        (r) => r.textContent.includes('mtbg_github_updates')), 'no row for the updater');
    row.querySelector('td.o_data_cell').click();
    await wait(() => document.querySelector('.o_mgu_form'), 'no form');
    const form = document.querySelector('.o_mgu_form');
    if (!form.textContent.includes('Needs')) throw new Error('no Needs');
    if (!form.textContent.includes('19.0.99.0.0')) throw new Error('no releases in the form');
""")
        self.browser_js("/odoo/action-%d" % self.action.id, code, login="admin")


@tagged("post_install", "-at_install")
class TestAddRepository(TransactionCase):
    """0.4.1 (owner: "with the new updater I see no way to add a new repository"): one form for the list's
    repositories and any other, straight on to its access."""

    def setUp(self):
        super().setUp()
        self.Source = self.env["mtbg.github.source"]
        self.Source.with_context(active_test=False).search([]).unlink()
        self.Add = self.env["mtbg.github.source.add"]
        keys = patch.object(store, "generate_key", return_value=None)
        keys.start()
        self.addCleanup(keys.stop)

    def test_a_listed_repository(self):
        choices = dict(self.Add._known_selection())
        self.assertIn("bikeshop", choices)
        self.assertIn("other", choices)
        action = self.Add.create({"known": "bikeshop"}).action_add()
        source = self.Source.search([("code", "=", "bikeshop")])
        self.assertEqual((source.name, source.subdir), ("Bike Shop", "addons"))
        self.assertEqual((action["res_model"], action["res_id"], action["target"]),
                         ("mtbg.github.source", source.id, "new"), "straight on to its access")
        self.assertNotIn("bikeshop", dict(self.Add._known_selection()), "offered once")

    def test_another_repository(self):
        self.Add.create({"known": "other", "name": "Side Shop", "url": "git@github.com:o/side-shop.git",
                         "subdir": "addons"}).action_add()
        self.assertTrue(self.Source.search([("code", "=", "side_shop")]))

    def test_opened_from_the_screens(self):
        self.assertEqual(self.Add.action_mgu_open()["res_model"], "mtbg.github.source.add")
        board = self.env.ref("mtbg_github_updates.view_mgu_extensions_kanban").arch
        self.assertIn("action_mgu_source_add", board.replace(str(self.env.ref(
            "mtbg_github_updates.action_mgu_source_add").id), "action_mgu_source_add"))


@tagged("post_install", "-at_install")
class TestAddRepositoryInBrowser(HttpCase):

    def test_buttons_on_the_board_and_activity(self):
        code = """
const wait = (test, what) => new Promise((ok, ko) => { let n = 0; const t = setInterval(() => {
    const v = test(); if (v) { clearInterval(t); ok(v); } else if (++n > 150) { clearInterval(t); ko(new Error(what)); } }, 100); });
(async () => {
    const button = await wait(() => [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Add a repository')), 'no Add a repository button');
    button.click();
    await wait(() => document.querySelector('.modal .o_form_view'), 'the button opened no form');
    if (!document.querySelector('.modal').textContent.includes('Another repository')) throw new Error('no other-repository choice');
    console.log('test successful');
})().catch((e) => console.error('test failed: ' + e.message));
"""
        board = self.env.ref("mtbg_github_updates.action_mgu_extensions")
        self.browser_js("/odoo/action-%d" % board.id, code, login="admin")
        self.browser_js("/odoo/updates", code, login="admin")
