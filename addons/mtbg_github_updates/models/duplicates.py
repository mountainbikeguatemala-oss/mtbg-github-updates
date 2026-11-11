"""A release that would install a module duplicating one already installed (0.4.2): e.g. a module renamed in a new
repository (``new_name`` beside ``old_name``, the same models and records under new names) offered as a NEW module
while the old one is still installed. Installed as is, both would hold the same data twice; such a change needs its
data moved first (a rename), never an install. Found from the release's own files, read from the mirror, nothing run:

* the models it defines (``_name`` without being one of its ``_inherit``) already defined by another installed module;
* its record ids (``<record id>``, ``<template id>``, ``<menuitem id>``, an access line's id): at least
  ``SHARE`` of them (and ``MIN_SHARED`` or more) are another installed module's record ids.

Such an install is refused; Update all holds back the updates that would bring it and goes on with the others.

A planned move is declared: the new module's manifest lists the modules it takes models and records over from
(``'mtbg_takes_over': ['old_module', ...]``, with its own pre_init hook moving their xml ids); those pairs are allowed,
every other duplicate is still refused.
"""
import ast
import csv
import io
import re

from odoo import _, api, models
from odoo.exceptions import UserError

from . import store

TAKES_OVER_KEY = "mtbg_takes_over"
SHARE = 0.5
MIN_SHARED = 5
XML_ID = re.compile(r'<(?:record|template|menuitem)\b[^>]*?\bid="([A-Za-z0-9_.]+)"')


def defined_models(files):
    """The models the Python files define (``_name`` set and not among the class's own ``_inherit``)."""
    found = set()
    for rel, text in files.items():
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            name, inherits = None, set()
            for item in node.body:
                if not isinstance(item, ast.Assign) or len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
                    continue
                target, value = item.targets[0].id, item.value
                if target == "_name" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    name = value.value
                elif target == "_inherit":
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        inherits = {value.value}
                    elif isinstance(value, (ast.List, ast.Tuple)):
                        inherits = {e.value for e in value.elts if isinstance(e, ast.Constant)}
            if name and name not in inherits:
                found.add(name)
    return found


def record_ids(files):
    """The record ids the data files declare (without a module prefix: this module's own)."""
    ids = set()
    for rel, text in files.items():
        if rel.endswith(".xml"):
            ids.update(i for i in XML_ID.findall(text) if "." not in i)
        elif rel.endswith(".csv"):
            try:
                rows = list(csv.reader(io.StringIO(text)))
            except csv.Error:
                continue
            if rows and rows[0] and rows[0][0] == "id":
                ids.update(row[0] for row in rows[1:] if row and row[0] and "." not in row[0])
    return ids


class MtbgGithubUpdateDuplicates(models.Model):
    _inherit = "mtbg.github.update"

    @api.model
    def _mtbg_duplicate_of(self, module, release):
        """Why installing ``release`` (of ``module``, not installed here) would duplicate an installed module: a
        sentence naming it, or ''."""
        Module = self.env["ir.module.module"].sudo()
        if not release or Module.search_count([("name", "=", module), ("state", "in", ("installed", "to upgrade"))]):
            return ""
        source = release.source_id
        try:
            files = store.release_files(source.code, source.url, source.subdir or "", module, release.tag)
        except store.StoreError:
            return ""
        installed = set(Module.search([("state", "in", ("installed", "to upgrade"))]).mapped("name"))
        # the modules it declares it takes over from (a planned move): never counted as duplicates
        try:
            declared = release._mtbg_manifest().get(TAKES_OVER_KEY) or []
        except store.StoreError:
            declared = []
        installed -= {name for name in declared if isinstance(name, str)}
        # its models, already defined by an installed module (the module that created the model)
        names = defined_models(files)
        if names:
            self.env.cr.execute("""
                SELECT m.model, d.module FROM ir_model m
                  JOIN ir_model_data d ON d.model = 'ir.model' AND d.res_id = m.id
                 WHERE m.model = ANY(%s) AND d.module = ANY(%s) AND d.module != %s
            """, [sorted(names), sorted(installed), module])
            owners = {}
            for model, owner in self.env.cr.fetchall():
                owners.setdefault(owner, set()).add(model)
            for owner, taken in sorted(owners.items()):
                defined_there = self._mtbg_defines(owner, taken)
                if defined_there:
                    return _("%(module)s would install the models %(models)s that %(owner)s already has: it looks "
                             "like %(owner)s renamed. Its data must be moved first (a rename), not installed beside "
                             "it.", module=module, owner=owner, models=", ".join(sorted(defined_there)[:5]))
        # its records: most of them another installed module's
        ids = record_ids(files)
        if len(ids) >= MIN_SHARED:
            self.env.cr.execute("""
                SELECT module, count(*) FROM ir_model_data
                 WHERE name = ANY(%s) AND module = ANY(%s) AND module != %s
                 GROUP BY module ORDER BY count(*) DESC LIMIT 1
            """, [sorted(ids), sorted(installed), module])
            row = self.env.cr.fetchone()
            if row and row[1] >= MIN_SHARED and row[1] >= SHARE * len(ids):
                return _("%(module)s has %(shared)s of its %(total)s records under the same names as %(owner)s: it "
                         "looks like %(owner)s renamed. Its data must be moved first (a rename), not installed beside "
                         "it.", module=module, owner=row[0], shared=row[1], total=len(ids))
        return ""

    @api.model
    def _mtbg_defines(self, owner, model_names):
        """Of ``model_names``, those ``owner`` defines (its model's first xml id: it created the model), not merely
        extends."""
        self.env.cr.execute("""
            SELECT m.model FROM ir_model m
             WHERE m.model = ANY(%s) AND (
                SELECT d.module FROM ir_model_data d WHERE d.model = 'ir.model' AND d.res_id = m.id
                 ORDER BY d.id LIMIT 1) = %s
        """, [sorted(model_names), owner])
        return {row[0] for row in self.env.cr.fetchall()}

    @api.model
    def _mtbg_check_duplicates(self, everything):
        """No batch installs a module duplicating an installed one."""
        problems = [reason for module, release, _by in everything
                    for reason in [self._mtbg_duplicate_of(module, release)] if reason]
        if problems:
            raise UserError(_("No update was made:\n%s", "\n".join(problems)))

    @api.model
    def _mtbg_hold_back(self, plan):
        """Update all: (the plan without the updates that would bring such a module, [(module, reason)] held back)."""
        kept, held = [], []
        for module, release in plan:
            try:
                everything = self.with_context(mtbg_no_duplicate_check=True)._mtbg_full_plan([(module, release)])
            except UserError:
                kept.append((module, release))  # (its own problem is reported by the whole plan's check)
                continue
            reason = next((r for m, rel, _by in everything for r in [self._mtbg_duplicate_of(m, rel)] if r), "")
            if reason:
                held.append((module, reason))
            else:
                kept.append((module, release))
        return kept, held

    @api.model
    def _mtbg_full_plan(self, plan):
        # (every batch is checked once its whole content is known; Update all has held such updates back first)
        everything = super()._mtbg_full_plan(plan)
        if not self.env.context.get("mtbg_no_duplicate_check"):
            self.with_context(mtbg_no_duplicate_check=True)._mtbg_check_duplicates(everything)
        return everything
