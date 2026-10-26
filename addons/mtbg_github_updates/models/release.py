"""Releases and packages.

* A release: one module's tag ``<module>-<version>`` (its notes: the tag's message and the CHANGELOG.md section of
  that version).
* A package: several modules released together, like a batch push. It is an annotated tag
  ``release-<repo>-<YYYYMMDD>-<n>`` on the batch commit (its first line the title, the rest its notes; its modules:
  the module tags on that same commit); a commit carrying two module tags or more and no release tag is a package
  too, untitled (the commit's subject)."""
import os

from odoo import _, api, fields, models

from . import store


class MtbgGithubRelease(models.Model):
    _name = "mtbg.github.release"
    _description = "Extension release"
    _order = "module, version_sort desc"
    _rec_name = "version"

    source_id = fields.Many2one("mtbg.github.source", required=True, ondelete="cascade", index=True)
    module = fields.Char(required=True, index=True)
    version = fields.Char(required=True)
    version_sort = fields.Char(compute="_compute_version_sort", store=True, index=True)
    tag = fields.Char(required=True)
    commit = fields.Char(index=True)
    date = fields.Datetime()
    notes = fields.Text(string="Release Notes")
    changelog = fields.Text(string="Changes", help="The CHANGELOG.md section of this version.")
    in_store = fields.Boolean(string="Downloaded", compute="_compute_state")
    is_active = fields.Boolean(string="In Use", compute="_compute_state")

    _tag_unique = models.Constraint("UNIQUE(source_id, tag)", "A release is listed once per source.")

    @api.depends("version")
    def _compute_version_sort(self):
        for release in self:
            release.version_sort = ".".join("%06d" % part for part in store.version_key(release.version))

    def _compute_state(self):
        for release in self:
            try:
                folder = os.path.join(store.version_dir(release.module, release.version), release.module)
            except store.StoreError:
                release.in_store = release.is_active = False
                continue
            release.in_store = os.path.isdir(folder)
            release.is_active = store.active_target(release.module) == os.path.realpath(folder)

    def _mtbg_manifest(self):
        self.ensure_one()
        source = self.source_id
        return store.release_manifest(source.code, source.url, source.subdir or "", self.module, self.tag)

    def _mtbg_text(self):
        """What is new in this release, for the page: its CHANGELOG section, else the tag's message."""
        self.ensure_one()
        return self.changelog or self.notes or ""


class MtbgGithubPackage(models.Model):
    _name = "mtbg.github.package"
    _description = "Package release (several modules released together)"
    _order = "date desc, id desc"

    source_id = fields.Many2one("mtbg.github.source", required=True, ondelete="cascade", index=True)
    tag = fields.Char(help="Its release-* tag; empty: a commit with several module tags.")
    commit = fields.Char(required=True, index=True)
    name = fields.Char(string="Title", required=True)
    notes = fields.Text()
    date = fields.Datetime()
    release_ids = fields.Many2many("mtbg.github.release", string="Modules")

    _commit_unique = models.Constraint("UNIQUE(source_id, commit)", "A commit is one package.")

    @api.model
    def _mtbg_sync(self, source, package_tags, by_commit):
        """The packages of ``source`` from its release tags [(tag, commit, date, subject)] and the releases on each
        commit ({commit: releases}). A batch push stacks several commits: a release tag's package is every module tag
        after the previous release tag of the same repository name, up to it ((previous .. this]); the first one has
        only the tags on its own commit."""
        existing = {p.commit: p for p in self.search([("source_id", "=", source.id)])}
        Release = self.env["mtbg.github.release"]
        wanted, covered, previous = {}, set(), {}
        ordered = sorted(package_tags, key=lambda t: (store.PACKAGE_RE.match(t[0])["repo"],
                                                      store.PACKAGE_RE.match(t[0])["date"],
                                                      int(store.PACKAGE_RE.match(t[0])["n"])))
        for tag, commit, date, subject in ordered:
            repo = store.PACKAGE_RE.match(tag)["repo"]
            try:
                commits = store.commits_between(source.code, source.url, previous.get(repo), commit)
            except store.StoreError:
                commits = {commit}
            previous[repo] = commit
            releases = Release
            for each in commits:
                releases |= by_commit.get(each, Release)
            if not releases:
                continue  # a release tag with no module tag of this repository
            try:
                text = store.tag_notes(source.code, source.url, tag)
            except store.StoreError:
                text = subject
            title, _sep, notes = text.partition("\n")
            covered |= commits
            wanted[commit] = {"tag": tag, "name": (title or subject or tag).strip()[:200], "notes": notes.strip(),
                              "date": fields.Datetime.to_datetime(date[:19].replace("T", " ")), "releases": releases}
        # older batches (no release tag): a commit carrying two module tags or more
        for commit, releases in by_commit.items():
            if commit in wanted or commit in covered or len(releases) < 2:
                continue
            subject = store.commit_subject(source.code, source.url, commit)
            wanted[commit] = {"tag": False, "name": (subject or _("Released together")).strip()[:200], "notes": "",
                              "date": max(releases.mapped("date")) if any(releases.mapped("date")) else False,
                              "releases": releases}
        for commit, values in wanted.items():
            values["release_ids"] = [(6, 0, values.pop("releases").ids)]
            if commit in existing:
                existing.pop(commit).write(values)
            else:
                self.create(dict(values, source_id=source.id, commit=commit))
        for package in existing.values():
            package.unlink()

    def _mtbg_plan(self):
        """What "Install this release" does on this site: [(module, release, what)], what = "update" (installed, an
        older version), "install" (not installed, but a module of this package that the others need) or a reason
        it is left out ("installed" newer or the same, "not here")."""
        self.ensure_one()
        Module = self.env["ir.module.module"].sudo()
        installed = {m.name: m for m in Module.search([("name", "in", self.release_ids.mapped("module")),
                                                       ("state", "in", ("installed", "to upgrade"))])}
        rows, newest = [], {}
        for release in self.release_ids:  # a batch may carry several versions of a module: its newest
            if release.module not in newest or \
                    store.version_key(release.version) > store.version_key(newest[release.module].version):
                newest[release.module] = release
        for release in sorted(newest.values(), key=lambda r: r.module):
            module = installed.get(release.module)
            if not module:
                rows.append((release.module, release, "not here"))
                continue
            # what this site has (the code loaded, the database's), never the earlier updaters' store links
            if store.version_key(release.version) > store.version_key(module._mgu_current()):
                rows.append((release.module, release, "update"))
            else:
                rows.append((release.module, release, "same"))
        return rows
