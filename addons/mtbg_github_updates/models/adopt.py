"""Taking over from the updaters before this one: the Tours updater (``mtbg_updates_dir``, default
<data_dir>/mtbg_updates) and the Communication Flow Designer's (``cfd_updates_dir``, default <data_dir>/cfd_updates).

Nothing is reinstalled and the database is not upgraded: each version they run is copied, as it is, into this store
and linked here; their update sources (repository, modules folder, patterns) come along with their deploy keys (the
same server, the same repositories: no new key to add in GitHub), and their history is copied in. The server then
restarts and loads every module from this store (its active folder first in addons_path). Their stores are left as
they were: taking this store's line out of addons_path and restarting goes back to them (the migration's rollback),
until they are archived.
"""
import logging
import os
import shutil
import time
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

from . import store

_logger = logging.getLogger(__name__)
# the updaters taken over: (key, label, configuration key, default folder name, source model, history table)
OLD = (
    ("tours", "Tours", "mtbg_updates_dir", "mtbg_updates", "mtbg.update.source", "mtbg_update_log"),
    ("cfd", "Communication Flow Designer", "cfd_updates_dir", "cfd_updates", "cfd.update.source", "cfd_update_log"),
)


def old_root(conf_key, folder):
    return config.get(conf_key) or os.path.join(config["data_dir"], folder)


def old_links(root):
    """{module: version folder} an old store links to."""
    active = os.path.join(root, "store", "active")
    links = {}
    try:
        names = os.listdir(active)
    except OSError:
        return links
    for name in names:
        link = os.path.join(active, name)
        if store.MODULE_RE.match(name) and os.path.islink(link):
            target = os.path.realpath(link)
            if os.path.isfile(os.path.join(target, "__manifest__.py")):
                links[name] = target
    return links


class MtbgGithubBatch(models.Model):
    _inherit = "mtbg.github.batch"

    @api.model
    def mtbg_adopt_preview(self):
        """What taking over would do: per earlier updater, the versions copied here, the sources and history."""
        self.env["mtbg.github.update"]._mtbg_check_admin()
        mine = set(store.active_modules())
        updaters = []
        for key, label, conf_key, folder, source_model, table in OLD:
            root = old_root(conf_key, folder)
            links = old_links(root)
            modules = []
            for module, target in sorted(links.items()):
                version = os.path.basename(os.path.dirname(target))
                manifest = store.folder_manifest(target)
                ok = store.VERSION_RE.match(version) and manifest.get("version") == version
                # in this store already (taken over, or updated here since): never taken from the old store again
                modules.append({"module": module, "version": version, "done": module in mine,
                                "problem": "" if ok else _("its folder says version %s", manifest.get("version"))})
            sources = []
            if source_model in self.env:
                sources = [{"name": s.name, "url": s.url} for s in self.env[source_model].sudo().search([])]
            updaters.append({"key": key, "label": label, "root": root, "modules": modules, "sources": sources})
        return {"updaters": updaters, "ready": store.in_addons_path(), "active": store.active_dir(),
                "todo": sum(1 for u in updaters for m in u["modules"] if not m["done"] and not m["problem"])}

    @api.model
    def mtbg_adopt(self):
        """Take over: copy, link, sources, keys, history; then restart (no upgrade: the code is the same)."""
        Update = self.env["mtbg.github.update"]
        Update._mtbg_check_ready()
        preview = self.mtbg_adopt_preview()
        Update._mtbg_check_shadows([m["module"] for u in preview["updaters"] for m in u["modules"] if not m["problem"]])
        self._mtbg_adopt_sources()
        name = uuid.uuid4().hex[:12]
        header = self.create({"name": name, "kind": "adopt", "title": _("Taken over from the earlier updaters")})
        lines = Update.browse()
        for key, label, conf_key, folder, _model, _table in OLD:
            for module, target in sorted(old_links(old_root(conf_key, folder)).items()):
                version = os.path.basename(os.path.dirname(target))
                if store.active_target(module):
                    continue  # in this store already: an older copy in the old store must not come back
                commit = ""
                try:
                    with open(os.path.join(os.path.dirname(target), store.RELEASE_MARK)) as handle:
                        commit = handle.read().strip()
                except OSError:
                    pass
                try:
                    copied = store.adopt_version(module, version, target, commit)
                except (store.StoreError, OSError) as error:
                    _logger.warning("Updates: %s %s not taken over: %s", module, version, error)
                    continue
                before = store.active_target(module)
                store.switch(module, copied)
                lines |= Update.create({
                    "batch": name, "batch_id": header.id, "module": module, "kind": "adopt",
                    "from_version": version, "to_version": version, "previous_target": before or False,
                    "target": copied, "switched_at": time.time(),
                    "message": _("From the %s updater's store.", label)})
        self._mtbg_adopt_history()
        if not lines:
            header.message = _("Nothing to take over.")
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"message": _("Nothing to take over."), "type": "info"}}
        Update._mtbg_commit()
        lines._mtbg_trigger_and_restart()
        return {"type": "ir.actions.client", "tag": "mtbg_github_progress", "params": {"batch": name},
                "name": _("Taking over")}

    @api.model
    def _mtbg_adopt_sources(self):
        """The earlier updaters' sources, with their deploy keys and tokens (by code; an existing one is kept)."""
        Source = self.env["mtbg.github.source"].with_context(active_test=False)
        for key, _label, conf_key, folder, source_model, _table in OLD:
            if source_model not in self.env:
                continue
            for old in self.env[source_model].sudo().with_context(active_test=False).search([]):
                existing = Source.search([]).filtered(lambda s: s._mtbg_is(old.url))
                code = old.code if not Source.search_count([("code", "=", old.code)]) else "%s_%s" % (key, old.code)
                if existing:
                    existing = existing[:1]
                    if not existing.module_patterns and old.module_patterns:
                        existing.module_patterns = old.module_patterns
                    if old.active and not existing.active:
                        existing.active = True  # archived in one earlier updater, in use in the other
                    code = existing.code
                else:
                    Source.create({"name": old.name, "code": code, "url": old.url, "subdir": old.subdir or "",
                                   "module_patterns": old.module_patterns, "active": old.active,
                                   "sequence": old.sequence if "sequence" in old._fields else 10})
                self._mtbg_adopt_key(old_root(conf_key, folder), old.code, code)

    @api.model
    def _mtbg_adopt_key(self, root, old_code, code):
        store.ensure_dirs()
        for suffix in ("", ".pub", ".token"):
            source = os.path.join(root, "keys", old_code + suffix)
            target = store.path("keys", code + suffix)
            if os.path.isfile(source) and not os.path.exists(target):
                shutil.copy2(source, target)
                os.chmod(target, 0o600 if suffix != ".pub" else 0o644)
        known = os.path.join(root, "known_hosts")
        if os.path.isfile(known) and not os.path.exists(store.path("known_hosts")):
            shutil.copy2(known, store.path("known_hosts"))

    @api.model
    def _mtbg_adopt_history(self):
        """The earlier updaters' history, as "Earlier updater" lines (once)."""
        Update = self.env["mtbg.github.update"].sudo()
        if Update.search_count([("kind", "=", "imported")]):
            return
        cr = self.env.cr
        for key, label, _conf, _folder, _model, table in OLD:
            cr.execute("SELECT to_regclass(%s)", (table,))
            if not cr.fetchone()[0]:
                continue
            cr.execute("SELECT batch, module, from_version, to_version, state, message, create_date "  # noqa: S608
                       "FROM %s ORDER BY id" % table)
            rows = cr.fetchall()
            if not rows:
                continue
            header = self.create({"name": "imported-%s" % key, "kind": "imported",
                                  "title": _("History of the %s updater", label)})
            for batch, module, from_version, to_version, state, message, created in rows:
                Update.create({"batch": "%s-%s" % (key, batch or ""), "batch_id": header.id, "module": module,
                               "kind": "imported", "from_version": from_version, "to_version": to_version,
                               "state": state if state in dict(Update._fields["state"].selection) else "applied",
                               "message": "%s%s" % (fields.Datetime.to_string(created) + " · " if created else "",
                                                    message or "")})

    @api.model
    def mtbg_adoption_check(self):
        """After the restart: every module the earlier stores link is loaded from this store?"""
        from odoo.modules.module import get_module_path
        active = os.path.realpath(store.active_dir())
        rows = []
        for _key, label, conf_key, folder, _model, _table in OLD:
            for module in sorted(old_links(old_root(conf_key, folder))):
                path = get_module_path(module, display_warning=False) or ""
                rows.append({"module": module, "updater": label,
                             "here": os.path.realpath(os.path.dirname(path)) == active if path else False})
        return rows
