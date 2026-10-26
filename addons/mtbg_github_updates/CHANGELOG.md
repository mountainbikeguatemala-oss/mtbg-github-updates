# Changelog

Every version of this module, newest first. Tags: `git tag -l 'mtbg_github_updates-*'`.

## 19.0.0.1.5 · 2026-10-26

- The install dialog no longer lists a module both as coming along (a glue, a dependency) and as "left out: not on
  this site".
- The history says why a module came along as it is: "Required by <module>." for a dependency, else "Installed by
  itself, needed by ...", "Released with ...", "Kept with ..." (it read "Installed (required by installed by itself,
  needed by ...)").

## 19.0.0.1.4 · 2026-10-26

- **The organization's repositories, one click away** (owner, 2026-10-26): the updater knows the Tours, Website
  Blocks, Communication Flow Designer and Updates repositories; the page offers those this site does not read yet
  ("Add this source", never added on its own), and an update needing a module no source releases names them. (A site
  whose modules still came from the Tours repository had no Website Blocks source: the pack's release was not listed.)
- **A failed upgrade brings the batch's backup back** (batch 7 on DEV2 and DEV3): the upgrade's own process commits
  module by module, so a migration that ran (content_blocks 0.9.0's) stayed when a later module failed, and the
  switch-back to the old code could not load over it (the site's database and code out of step). Now the batch's
  backup is restored (database, files, code), as a rollback does, then the server restarts; the code alone goes back
  only without a backup. The site is in maintenance from before the backup, so nothing a visitor enters is lost.
- **Released together, installed together**: a batch taking one module of a package (a release tag, or a commit with
  several module tags) also takes that package's other modules this site has at an older version ("released with
  ..."), with what they need. Batch 7: the page-templates glue pulled from the Tours release came without the catalog
  0.6.0 its model came from.
- **A module that moved repository**: its newest version is its newest release from any source that may offer it,
  and it shows under that source (it showed only the old repository's tags until updated once).

## 19.0.0.1.3 · 2026-10-26

- **Never an older version** (found on DEV4, nothing installed): a package compared its modules with the Tours
  updater's store links (its own field, by mistake), so on a site loading its modules from elsewhere 16 old packages
  offered "Install this release", which would have put older versions first in addons_path. A module's current version
  is now what this site has (the code Odoo loads, or the database's, the newer), everywhere; and a batch with a
  version not newer than that is refused before anything changes (only Go back goes back).
- **A glue comes only with the batch it completes**: a module installing itself with others (auto_install) joined
  every plan when all its modules were installed; it now comes only when the batch brings or updates one of them.

## 19.0.0.1.2 · 2026-10-26

- **A restarted batch finishes by itself** (seen on DEV1 and DEV2: the take-over stayed "Restarting" until someone
  finished it): the scheduled job waited for every module of a take-over to be installed at its version, and a store
  links modules a database does not have installed (a retired one): a take-over, and a module not installed here,
  now need nothing after the restart. Opening the Updates page finishes such batches too (sites whose jobs are
  paused). A server restarted in place (the same process id) counts as restarted.

## 19.0.0.1.1 · 2026-10-26

- **One store, one database**: the first database to use a store writes its name in it (owner.json); another
  database (two sites sharing a data_dir) is refused on the page and by every action, with how to fix it (its own
  `mtbg_github_updates_dir`).
- **Take over leaves work in progress alone**: only the modules Odoo loads from an old store are copied; one loaded
  from another folder (a dev site's links after the old store) is listed "left as is" and never copied, so this
  store, first in addons_path, never hides it.

## 19.0.0.1.0 · 2026-10-26

- **One updater for every product** (owner, 2026-10-26): the Tours, the Communication Flow Designer and the website
  blocks pack update from one Updates page (Settings > Updates), on a site that has only one of them. Each product
  names its repository in its manifests (`mtbg_github_source`); a module is offered only from its own repository.
- **Packages**: a `release-<repo>-<YYYYMMDD>-<n>` tag covers every module tag since the previous one (a batch push of
  several commits): one "Install this release". The per-module view stays.
- Each update: backup + filestore snapshot, the release tag's version (checked against its manifest) into the store,
  the upgrade in a process of its own, a restart; new dependencies from any source, minimum versions honoured; a
  folder before the store with its own copy of a module stops the update; a failed upgrade switches back by itself.
- **Roll back an update**: code, database and files as before it, after a preview of the data lost and the
  database's name typed; the database before is kept 7 days. **Go back (code only)** for one module, when safe.
  Backups kept for the last 10 updates or 30 days.
- **Take over** the Tours and CFD updaters' stores: their versions copied as they are (no reinstall, no upgrade),
  with their sources, deploy keys and history; their stores stay untouched (taking this store's line out of
  addons_path goes back to them).
