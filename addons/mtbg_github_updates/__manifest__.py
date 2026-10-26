{
    "name": "Updates from GitHub",
    "version": "19.0.0.1.2",
    "category": "Administration",
    "summary": "One Updates page for every product's extensions: from their GitHub releases, one module or a whole "
               "package, with backup, automatic and manual rollback",
    "description": """
One updater for every product (the Tours, the Communication Flow Designer, the website blocks pack...), on one
Updates page (Settings > Updates), working on a site that has only one of them.

* Each product says which GitHub repository it comes from: ``'mtbg_github_source': {'repo': ..., 'subdir': 'addons'}``
  in its modules' manifests. A source is added by itself for each one an installed module names, with a read-only
  deploy key (or token) per source. A module is only offered from its own repository.
* Releases: a module's tag ``<module>-<version>``. Packages: several modules released together, a
  ``release-<repo>-<YYYYMMDD>-<n>`` tag on the batch commit (or a commit with several module tags): one "Install
  this release" for all of them.
* Each update: a database backup and a filestore snapshot; the version from its release tag (checked) into the
  store; the database upgraded in a process of its own; the site restarted. New dependencies come along, from any
  source; minimum versions are honoured. A failed upgrade switches back by itself.
* Roll back an update: the code, the database and the files as they were before it (the data entered since is
  lost: the page says how much first). Go back (code only) for one module, when it is safe.
* Takes over the Tours and CFD updaters' stores without reinstalling anything.

The site loads the chosen versions from ``<data_dir>/mtbg_github_updates/store/active``: that folder comes FIRST in
the server's ``addons_path`` (the Updates page shows the exact line).
""",
    "author": "MTB Guatemala",
    "license": "LGPL-3",
    "depends": ["base", "web"],
    "mtbg_github_source": {"repo": "git@github.com:mountainbikeguatemala-oss/mtbg-github-updates.git",
                           "subdir": "addons"},
    "data": [
        "security/ir.model.access.csv",
        "data/cron.xml",
        "views/updates_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "mtbg_github_updates/static/src/**/*",
        ],
    },
    "installable": True,
}
