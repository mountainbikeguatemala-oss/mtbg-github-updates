# Changelog

Every version of this module, newest first. Tags: `git tag -l 'mtbg_github_updates-*'`.

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
