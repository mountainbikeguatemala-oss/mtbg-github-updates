"""The GitHub repositories the extensions come from. Each product says which one is its own: a module's manifest
carries ``'mtbg_github_source': {'repo': 'git@github.com:owner/repository.git', 'subdir': 'addons'}``. A source is
added by itself for each repository an installed module names, so a site that has only one product (a CFD-only
store) sees only that product's source. A module is offered only from the repository its own manifest names (in
the release: a module that moved repository is then offered from its new one); a module without the key (released
before it) goes by the source's module patterns."""
import fnmatch
import logging
import re
from collections.abc import Mapping

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import store

_logger = logging.getLogger(__name__)
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
REPO_RE = re.compile(r"^(git@github\.com:|https://github\.com/)(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(\.git)?$")
MANIFEST_KEY = "mtbg_github_source"
# the organization's repositories (owner, 2026-10-26): the page offers those this site has no source for, with one
# click (never added on their own); patterns: for releases made before their manifests named their repository
KNOWN_SOURCES = (
    {"key": "tours", "name": "MTBG Tours", "code": "tours", "subdir": "addons",
     "url": "git@github.com:mountainbikeguatemala-oss/mtbg-tours.git", "module_patterns": "mtbg_tours*"},
    {"key": "blocks", "name": "Website Blocks", "code": "website_blocks", "subdir": "addons",
     "url": "git@github.com:mountainbikeguatemala-oss/mtbg-website-blocks.git",
     "module_patterns": "mtbg_tours_bullets,mtbg_tours_content_blocks,mtbg_website_*,website_review_sync"},
    {"key": "cfd", "name": "Communication Flow Designer", "code": "cfd", "subdir": "",
     "url": "git@github.com:mountainbikeguatemala-oss/communication-flow-designer.git",
     "module_patterns": "communication_flow_designer*,cfd_*,google_connector,meta_connector"},
    {"key": "updates", "name": "Updates from GitHub", "code": "mtbg_github_updates", "subdir": "addons",
     "url": "git@github.com:mountainbikeguatemala-oss/mtbg-github-updates.git", "module_patterns": "mtbg_github_*"},
)
# extensions retired (replaced by others): their old tags stay on GitHub for the sites that run them, but they are
# never offered for a new install
RETIRED = ("mtbg_tours_customer360", "mtbg_tours_sms", "mtbg_tours_updates")


def repo_key(url):
    """The same repository whatever its spelling (ssh or https, with or without .git): owner/name, lowercase."""
    match = REPO_RE.match((url or "").strip())
    return ("%s/%s" % (match["owner"], match["name"])).lower() if match else (url or "").strip()


def declared_source(manifest):
    """(repo, subdir) a manifest names, or (None, None)."""
    spec = manifest.get(MANIFEST_KEY) if isinstance(manifest, Mapping) else None
    if not isinstance(spec, dict) or not spec.get("repo"):
        return None, None
    return spec["repo"], spec.get("subdir", "addons") or ""


class MtbgGithubSource(models.Model):
    _name = "mtbg.github.source"
    _description = "Update source (GitHub repository)"
    _order = "sequence, id"

    name = fields.Char(required=True)
    code = fields.Char(required=True, help="Short technical name (keys and mirror files are named after it).")
    short = fields.Char(string="Release Name", help="The repository's name in its package tags: release-<name>-...")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    url = fields.Char(string="Repository", required=True,
                      help="git@github.com:owner/repository.git, https://github.com/owner/repository (with a token), "
                           "or a local git folder (tests).")
    subdir = fields.Char(string="Modules Folder", default="addons",
                         help="Folder of the modules inside the repository; empty when they are at its root.")
    module_patterns = fields.Char(string="Modules (older releases)",
                                  help="For releases whose manifest does not name its repository yet: the modules "
                                       "this repository provides, comma separated; * matches anything.")
    declared = fields.Boolean(readonly=True, help="Added because an installed module names this repository.")
    public_key = fields.Text(compute="_compute_public_key",
                             help="Add it in GitHub: repository > Settings > Deploy keys > Add deploy key "
                                  "(leave “Allow write access” off).")
    has_key = fields.Boolean(compute="_compute_public_key")
    has_token = fields.Boolean(compute="_compute_public_key")
    last_check = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    release_ids = fields.One2many("mtbg.github.release", "source_id", string="Releases")
    package_ids = fields.One2many("mtbg.github.package", "source_id", string="Packages")
    release_count = fields.Integer(compute="_compute_release_count")

    _code_unique = models.Constraint("UNIQUE(code)", "Each update source needs its own code.")

    @api.constrains("code")
    def _check_code(self):
        for source in self:
            if not CODE_RE.match(source.code or ""):
                raise UserError(_("The code may only use lowercase letters, digits and _."))

    def _compute_public_key(self):
        for source in self:
            ok = bool(source.code and CODE_RE.match(source.code))
            source.public_key = store.public_key(source.code) if ok else ""
            source.has_key = bool(source.public_key)
            source.has_token = bool(ok and store.read_token(source.code))

    def _compute_release_count(self):
        for source in self:
            source.release_count = len(source.release_ids)

    def _mtbg_is(self, url):
        self.ensure_one()
        return repo_key(self.url) == repo_key(url)

    def _mtbg_pattern_match(self, module):
        self.ensure_one()
        return any(fnmatch.fnmatchcase(module, pattern.strip())
                   for pattern in (self.module_patterns or "").split(",") if pattern.strip())

    # ------------------------------------------------------------------ which repository a module comes from
    @api.model
    def _mtbg_installed_manifests(self):
        """{module: manifest} of the installed modules (the code the server loads)."""
        from odoo.modules.module import Manifest
        names = self.env["ir.module.module"].sudo().search([("state", "in", ("installed", "to upgrade"))]).mapped("name")
        manifests = {}
        for name in names:
            manifest = Manifest.for_addon(name, display_warning=False)
            if manifest:
                manifests[name] = manifest
        return manifests

    @api.model
    def _mtbg_source_of(self, module, manifest=None):
        """The source ``module`` comes from: the repository its manifest names (``manifest``: a release's; else the
        installed one's), else the first source whose patterns match it."""
        sources = self.search([])
        if manifest is None:
            from odoo.modules.module import Manifest
            manifest = Manifest.for_addon(module, display_warning=False) or {}
        repo, _subdir = declared_source(manifest)
        if repo:
            return sources.filtered(lambda s: s._mtbg_is(repo))[:1]
        return sources.filtered(lambda s: s._mtbg_pattern_match(module))[:1]

    @api.model
    def _mtbg_sync_declared(self):
        """A source for each repository an installed module names (once: an administrator may archive it)."""
        known = self.with_context(active_test=False).search([])
        seen = {}
        for module, manifest in self._mtbg_installed_manifests().items():
            repo, subdir = declared_source(manifest)
            if repo and repo_key(repo) not in seen:
                seen[repo_key(repo)] = (repo, subdir, module)
        created = self.browse()
        for key, (repo, subdir, module) in seen.items():
            if known.filtered(lambda s: s._mtbg_is(repo)):
                continue
            name = key.split("/")[-1]
            code = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")[:40] or "source"
            if not CODE_RE.match(code):
                code = "src_" + code
            base, n = code, 2
            while known.filtered(lambda s: s.code == code) or created.filtered(lambda s: s.code == code):
                code, n = "%s_%s" % (base, n), n + 1
            created |= self.create({"name": name, "code": code, "url": repo, "subdir": subdir, "declared": True})
            _logger.info("Updates: source %s added (named by %s)", repo, module)
        return created

    @api.model
    def _mtbg_known_missing(self):
        """The organization's repositories this site has no source for (archived ones count as there)."""
        sources = self.with_context(active_test=False).search([])
        return [known for known in KNOWN_SOURCES if not sources.filtered(lambda s: s._mtbg_is(known["url"]))]

    @api.model
    def mtbg_add_known(self, key):
        """Add one of the organization's repositories as a source (then: its deploy key, Check GitHub)."""
        self._mtbg_check_admin()
        known = next((k for k in self._mtbg_known_missing() if k["key"] == key), None)
        if not known:
            raise UserError(_("This repository is a source already."))
        code, n = known["code"], 2
        while self.with_context(active_test=False).search_count([("code", "=", code)]):
            code, n = "%s_%s" % (known["code"], n), n + 1
        source = self.create({"name": known["name"], "code": code, "url": known["url"], "subdir": known["subdir"],
                              "module_patterns": known["module_patterns"]})
        return source.id

    @api.model
    def _mtbg_known_hint(self):
        """For a missing dependency: the organization's repositories this site does not read yet."""
        missing = self._mtbg_known_missing()
        if not missing:
            return ""
        return " " + _("It may be released in a repository this site does not read yet: %s (Settings › Updates › "
                       "Add this source).", ", ".join(k["name"] for k in missing))

    # ------------------------------------------------------------------ buttons
    def _mtbg_check_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only administrators can manage updates."))

    def action_generate_key(self):
        self._mtbg_check_admin()
        self.ensure_one()
        comment = "odoo-%s-%s-read-only" % (self.env.cr.dbname, self.code)
        store.generate_key(self.code, comment)
        return self._mtbg_notify(_("New key created. Add its public key to GitHub as a deploy key (read only), "
                                   "then Test connection."), "info")

    def mtbg_set_token(self, token):
        """A read-only access token for an https repository (empty: removed). Kept in a file on the server."""
        self._mtbg_check_admin()
        self.ensure_one()
        try:
            store.save_token(self.code, (token or "").strip())
        except store.StoreError as error:
            raise UserError(str(error)) from error
        return True

    def action_test_connection(self):
        self._mtbg_check_admin()
        self.ensure_one()
        try:
            count = store.remote_tag_count(self.code, self.url)
        except store.StoreError as error:
            self.last_error = str(error)
            return self._mtbg_notify(_("Cannot read %(repo)s: %(error)s", repo=self.url, error=error), "danger")
        self.last_error = False
        return self._mtbg_notify(_("Connected: %s release tags on GitHub.", count), "success")

    def action_check_updates(self):
        """Fetch every source and list its releases and packages (Check GitHub)."""
        self._mtbg_check_admin()
        self._mtbg_sync_declared()
        sources = self or self.search([])
        problems = []
        for source in sources:
            try:
                source._mtbg_refresh()
            except store.StoreError as error:
                source.last_error = str(error)
                problems.append("%s: %s" % (source.name, error))
        if problems:
            return self._mtbg_notify(_("Some sources could not be read:\n%s", "\n".join(problems)), "warning")
        new = self.env["ir.module.module"].search([("mgu_update", "=", True)])
        return self._mtbg_notify(
            _("%s extensions have a newer version.", len(new)) if new else _("Everything is up to date."), "success")

    def _mtbg_refresh(self):
        """Fetch the mirror; its module tags become releases (only the modules this repository provides) and its
        release-* tags (or a commit carrying several module tags) packages."""
        self.ensure_one()
        store.fetch(self.code, self.url)
        Release = self.env["mtbg.github.release"].sudo()
        existing = {r.tag: r for r in self.release_ids}
        seen, by_commit, package_tags = set(), {}, []
        for tag, commit, date, subject, annotated in store.list_tags(self.code, self.url):
            if store.PACKAGE_RE.match(tag):
                package_tags.append((tag, commit, date, subject))
                continue
            match = store.TAG_RE.match(tag)
            if not match:
                continue
            module, version = match["module"], match["version"]
            values = {"commit": commit, "date": fields.Datetime.to_datetime(date[:19].replace("T", " "))}
            release = existing.get(tag)
            if not release:
                try:
                    manifest = store.release_manifest(self.code, self.url, self.subdir or "", module, tag)
                except store.StoreError:
                    continue  # not a module of this repository's modules folder
                repo, _subdir = declared_source(manifest)
                if repo and not self._mtbg_is(repo):
                    continue  # its manifest names another repository (moved there): offered from that one
                if not repo and not self._mtbg_pattern_match(module):
                    continue
                try:
                    notes = store.tag_notes(self.code, self.url, tag)
                except store.StoreError:
                    notes = ""
                if not notes or notes.strip() == tag:  # a tag saying only its name: the commit's message says it
                    notes = store.commit_message(self.code, self.url, commit)
                changes = store.changelog_section(self.code, self.url, self.subdir or "", module, tag, version)
                release = Release.create(dict(values, source_id=self.id, tag=tag, module=module, version=version,
                                              notes=notes, changelog=changes))
            else:
                if not release.notes or release.notes.strip() == tag:  # listed before the commit's message was read
                    values["notes"] = store.commit_message(self.code, self.url, commit) or release.notes
                release.write(values)
            seen.add(tag)
            by_commit.setdefault(commit, Release)
            by_commit[commit] |= release
        for tag, release in existing.items():
            if tag not in seen:
                release.unlink()  # a tag removed on GitHub is no longer offered (a version in use stays in the store)
        self.env["mtbg.github.package"]._mtbg_sync(self, package_tags, by_commit)
        self.write({"last_check": fields.Datetime.now(), "last_error": False})

    @api.model
    def _cron_check(self):
        self._mtbg_sync_declared()
        for source in self.search([]):
            try:
                source._mtbg_refresh()
            except store.StoreError as error:
                source.last_error = str(error)
                _logger.warning("Update source %s could not be read: %s", source.code, error)

    def _mtbg_notify(self, message, kind):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"message": message, "type": kind, "sticky": kind in ("danger", "warning")}}
