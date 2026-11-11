"""0.4.2: a release that would install a duplicate of an installed module (a renamed module offered as new beside
the old one) is refused; Update all holds back the updates that would bring it; a normal new install goes on."""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import duplicates, store

DUPLICATE_PY = """
from odoo import models
class Category(models.Model):
    _name = 'res.partner.category'
    _description = 'Tags, renamed'
class Extension(models.Model):
    _name = 'res.partner'
    _inherit = 'res.partner'
"""
FRESH_PY = """
from odoo import models
class Thing(models.Model):
    _name = 'mgu.dup.thing'
    _description = 'A new thing'
class Partner(models.Model):
    _inherit = ['res.partner']
"""


@tagged("post_install", "-at_install")
class TestDuplicateInstalls(TransactionCase):

    def setUp(self):
        super().setUp()
        Source = self.env["mtbg.github.source"]
        self.source = Source.create({"name": "Dup", "code": "dup_src", "url": "git@github.com:o/dup.git"})
        Release = self.env["mtbg.github.release"]
        self.renamed = Release.create({"source_id": self.source.id, "module": "mgu_renamed_tags",
                                       "version": "19.0.1.0.0", "tag": "mgu_renamed_tags-19.0.1.0.0"})
        self.copied = Release.create({"source_id": self.source.id, "module": "mgu_copied_views",
                                      "version": "19.0.1.0.0", "tag": "mgu_copied_views-19.0.1.0.0"})
        self.fresh = Release.create({"source_id": self.source.id, "module": "mgu_fresh",
                                     "version": "19.0.1.0.0", "tag": "mgu_fresh-19.0.1.0.0"})
        self.env.cr.execute("SELECT name FROM ir_model_data WHERE module = 'base' AND model = 'ir.ui.view' "
                            "ORDER BY id LIMIT 8")
        base_views = [row[0] for row in self.env.cr.fetchall()]
        copied_xml = "<odoo>%s</odoo>" % "".join('<record id="%s" model="ir.ui.view"/>' % i for i in base_views)
        files = {
            "mgu_renamed_tags": {"models/tags.py": DUPLICATE_PY},
            "mgu_copied_views": {"views/views.xml": copied_xml, "models/x.py": "x = 1\n"},
            "mgu_fresh": {"models/thing.py": FRESH_PY, "views/views.xml": '<odoo><record id="view_mgu_dup_thing" '
                                                                          'model="ir.ui.view"/></odoo>'},
        }
        reader = patch.object(store, "release_files", side_effect=lambda code, url, sub, module, tag: files[module])
        reader.start()
        self.addCleanup(reader.stop)
        self.Update = self.env["mtbg.github.update"]
        self.manifests = {}
        Release = type(self.env["mtbg.github.release"])
        manifest = patch.object(Release, "_mtbg_manifest", autospec=True,
                                side_effect=lambda rec: self.manifests.get(rec.module, {"depends": []}))
        manifest.start()
        self.addCleanup(manifest.stop)

    def test_parsing(self):
        self.assertEqual(duplicates.defined_models({"a.py": DUPLICATE_PY}), {"res.partner.category"},
                         "an _inherit of its own _name is an extension, not a definition")
        self.assertEqual(duplicates.defined_models({"a.py": FRESH_PY}), {"mgu.dup.thing"})
        self.assertEqual(duplicates.record_ids({"v.xml": '<record id="a" model="x"/><menuitem id="b"/>'
                                                         '<record id="base.c" model="x"/>',
                                                "s.csv": "id,name\naccess_x,x\n"}), {"a", "b", "access_x"})

    def test_a_renamed_module_is_refused(self):
        reason = self.Update._mtbg_duplicate_of("mgu_renamed_tags", self.renamed)
        self.assertIn("base", reason)
        self.assertIn("res.partner.category", reason)
        with self.assertRaisesRegex(UserError, "renamed"):
            self.Update._mtbg_check_duplicates([("mgu_renamed_tags", self.renamed, None)])

    def test_a_declared_takeover_is_allowed(self):
        """A planned move (the new module lists where it takes models from) installs; any other owner still counts."""
        self.manifests["mgu_renamed_tags"] = {"depends": ["base"], "mtbg_takes_over": ["base"]}
        self.assertEqual(self.Update._mtbg_duplicate_of("mgu_renamed_tags", self.renamed), "")
        self.Update._mtbg_check_duplicates([("mgu_renamed_tags", self.renamed, None)])
        self.manifests["mgu_renamed_tags"] = {"depends": ["base"], "mtbg_takes_over": ["web"]}
        self.assertIn("base", self.Update._mtbg_duplicate_of("mgu_renamed_tags", self.renamed),
                      "declared from another module: still refused")
        self.manifests["mgu_copied_views"] = {"mtbg_takes_over": ["base"]}
        self.assertEqual(self.Update._mtbg_duplicate_of("mgu_copied_views", self.copied), "")

    def test_records_under_another_modules_names_are_refused(self):
        self.assertIn("records under the same names as base",
                      self.Update._mtbg_duplicate_of("mgu_copied_views", self.copied))

    def test_a_normal_new_install_goes_on(self):
        self.assertEqual(self.Update._mtbg_duplicate_of("mgu_fresh", self.fresh), "")
        self.Update._mtbg_check_duplicates([("mgu_fresh", self.fresh, None)])

    def test_an_installed_module_is_never_a_duplicate_of_itself(self):
        updater = self.env["mtbg.github.release"].create({
            "source_id": self.source.id, "module": "base", "version": "19.0.99.0.0", "tag": "base-19.0.99.0.0"})
        self.assertEqual(self.Update._mtbg_duplicate_of("base", updater), "", "an update, not an install")

    def test_update_all_holds_back_what_would_bring_it(self):
        bringer = self.env["mtbg.github.release"].create({
            "source_id": self.source.id, "module": "web", "version": "19.0.99.0.0", "tag": "web-19.0.99.0.0"})
        alone = self.env["mtbg.github.release"].create({
            "source_id": self.source.id, "module": "base_setup", "version": "19.0.99.0.0",
            "tag": "base_setup-19.0.99.0.0"})

        def full_plan(model, plan):
            module, release = plan[0]
            rows = [(module, release, None)]
            if module == "web":
                rows.append(("mgu_renamed_tags", self.renamed, "web"))
            return rows
        with patch.object(type(self.Update), "_mtbg_full_plan", autospec=True, side_effect=full_plan):
            kept, held = self.Update._mtbg_hold_back([("web", bringer), ("base_setup", alone)])
        self.assertEqual([m for m, _r in kept], ["base_setup"])
        self.assertEqual([m for m, _r in held], ["web"])
        self.assertIn("renamed", held[0][1])
