# Changelog

Every version of this module, newest first. Tags: `git tag -l 'mtbg_github_updates-*'`.

## 19.0.0.4.0 · 2026-11-11
- **The extensions board** (owner, 2026-11-11: "kanban style view for different sources / extensions and their
  dependencies and a list view with a side filter"; "different columns for different sources, card / list view"):
  Settings › Updates › Extensions. One column per source (repository), "Not from GitHub" last; each column's header
  has the source's Check GitHub, Access (its deploy key) and Test connection. A card per extension: the version
  running and the newest on GitHub, its status (Update, New, Up to date, Not from GitHub, Error after a failed or
  stuck update), the folder its code loads from (the Updates store, another addons folder, Odoo; "hidden behind
  another copy" when the store's version is not the one loaded), what it needs and what uses it, across
  repositories. Update…, Install… and Versions… open the Activity screen on that extension (the confirmation, the
  backup and the restart as before).
- The list of the same extensions (the Card / List switch), grouped by source; the side filter by source, status
  and folder; filters Installed, Not installed, Update ready, Hidden; group by folder path. A form per extension:
  versions, where from, needs and used by, its releases, Open on GitHub.
- The Updates menu: Extensions, Activity (the page as before: Update all, release packages, history, rollback,
  take-over), Release packages, Releases, Sources, History. A source's form has Check GitHub, Test connection, New
  deploy key and its public key to copy.
- Odoo's own kanban, list, form and search panel only: the updater still runs on a bare site and updates itself.
  The board's values are kept on the extensions and refreshed when the board opens and after Check GitHub.

## 19.0.0.3.3 · 2026-11-11
- **Bike Shop on the list** (owner, 2026-11-11: the shop could not add its new repository): More repositories offers
  Bike Shop (`mtbgt_bikeshop`, modules in `addons`).
- **Add a repository that is not listed**: under More repositories, its name, its GitHub address and the folder of
  its modules (administrators only); then its deploy key and Check GitHub, as for the others.
- **The moved modules' older releases point to their new repository**: the connectors are no longer said to be the
  Communication Flow Designer's (every release of theirs names its repository); Visits by Script, Tiled Gallery and
  Cloudflare Page Cache, whose older releases named none, are named by Website Blocks. A module named whole now wins
  over another source's pattern with * (it used to be the first source listed).

## 19.0.0.3.2 · 2026-11-11
- **Take over offers only this database's earlier updaters** (owner, 2026-11-11: the shop's Updates page offered "Tours: 44 versions. Communication Flow Designer: 10 versions"): two sites on one server share the data_dir, and the old updaters' default folders (`<data_dir>/mtbg_updates`, `<data_dir>/cfd_updates`) were the other site's. An earlier updater is now offered and taken over only when this database used it: its source model is loaded here, or its history table exists here with rows. A site that took over already loses nothing, and its own earlier updater is still offered as before. Tests: another site's store gives no banner and nothing to take; an empty history table doesn't count; history rows do, and the take-over runs.

## 19.0.0.3.1 · 2026-11-11
- No site or company names in the release (owner rule): author "MTBG Tours contributors". No behaviour change.

## 19.0.0.3.0 · 2026-11-10
- Adjustable backup schedule (owner, 2026-11-10: "we should have adjustable schedule for backups with schedules for
  daily, weekly, monthly etc and expire after"; and how often they are taken).
  - Fading rules (mtbg.offsite.rule), edited on Off-site backups › Edit the fading rules: each rule keeps one
    snapshot every N days (0 = every one) until it expires after M days (0 = never, the last rule only). A snapshot
    follows the first rule it has not outlived. Defaults are today's schedule; "Back to the default rules" restores
    them. Checks: no expiry under the 7-day bucket lock, the period shorter than the expiry, one forever rule at
    most, no two rules with the same expiry, at least one rule.
  - Take a snapshot every N hours / days / weeks; days and weeks from the set hour (site time).
  - The page shows the rules in words and a preview: about how many snapshots stay, and their size at today's
    sizes.
  - A snapshot's Kind is its rule's name (migration: the 0.2.0 codes become the names).

## 19.0.0.2.0 · 2026-11-10
- Off-site backups in Cloudflare R2 (owner, 2026-11-10: R2 replaces the Vultr snapshots; "we should backup every
  day - then reduce to one every 7 days then one every 30 days then one every 60 days, 180 days, 360 days fading
  over time"; yearly ones kept forever; "in the settings section we should see the different backups that have
  been made with links and sizes and timestamps"; "table should include current charges for storage as well and
  storage total used").
  - A daily snapshot (Settings › Backups: on/off and the hour, site time): a pg_dump plus the file list, with
    every uploaded file stored once under files/<db>/ and shared by every snapshot. A first upload of several GB
    goes on in the next job runs (7 minutes each) without sending a file twice. Verified: the dump's size in R2,
    the list, and every listed file present.
  - Fading: daily for 7 days, one per 7 days to 30, per 30 to 60, per 60 to 180, per 180 to 360, then one per
    360 days kept forever (the oldest of each period stays). Dropped snapshots, and files no kept snapshot lists,
    are deleted once older than the 7-day bucket lock.
  - Settings › Off-site backups: storage used (the whole bucket), current charges (R2 Standard $0.015/GB-month
    after the account's free 10 GB), snapshots kept, the bucket, Back up now, and every snapshot with its time,
    kind, database / files / total sizes, status and private 1-hour download links (database, file list).
  - R2 through Cloudflare Connector (a soft check, no dependency); without it the page says to install it.

## 19.0.0.1.9 · 2026-11-01

- **Only the last 3 backups are kept** (owner, 2026-11-01: www's pre-update backups reached 8.9 GB and filled the
  disk): after each backup the older ones (a dump and its filestore snapshot together) are deleted, each one logged;
  the daily clean-up keeps the same 3. An update whose backup is gone offers no rollback.
- **Room checked before the backup**: the database's size and a tenth must be free in the backups folder, else the
  update is refused before pg_dump: "Not enough disk space for a backup: X GB free, Y GB needed." (the filestore
  snapshot is hard links: no room).

## 19.0.0.1.8 · 2026-10-28

- Release notes folded by default (owner, 2026-10-28): an extension's notes open only if it was in the last update;
  each extension folds / unfolds its own (the arrow by its name); "Show all notes" / "Hide all notes" for all.

## 19.0.0.1.7 · 2026-10-28

- **Release notes, one line per change** (owner, 2026-10-28): the update's confirmation lists, under each extension
  it installs, what is new in that version; each extension's row shows its most recent notes. They come from the
  release's commit message when its tag says no more than its name (nothing typed by hand); trailers left out.
- A version number is copied to the clipboard on a click.
- The database ahead of the code: a module whose database already has a version the code loaded does not (DEV2:
  Communications 0.5.18 ran from a dev link, the store never got it) is offered that version; it was "up to date".
- An uninstalled glue (auto_install) whose modules are all installed is offered on "New on GitHub" (www:
  cfd_website_visitors could only come back with Apps › Update Apps List).

## 19.0.0.1.6 · 2026-10-26

- The Updates page opens on a family of extensions too (`action_mgu_open_updates(patterns, title)`, the page's
  params `modules` + `title`): its modules, the packages and new releases that have one of them, their history,
  "Update all" for them only; "Every extension" goes back. For the Tours Extensions card, the Logistics page and the
  Zoho page, now that the Tours Updates page retires (owner, 2026-10-26).
- The Tours Updates extension (mtbg_tours_updates) is retired: never offered for a new install.

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
