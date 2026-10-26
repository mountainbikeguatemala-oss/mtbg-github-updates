"""Where updates live on the server, and the git / file / database operations behind them (one store for every
product: the Tours, the Communication Flow Designer, the website blocks pack...).

    <mtbg_github_updates_dir>   (server configuration; default <data_dir>/mtbg_github_updates)
        keys/<source>, keys/<source>.pub    read-only deploy key of each source (private key: owner only)
        keys/<source>.token                 or a read-only access token (https repositories)
        known_hosts                         GitHub's host key, learnt on first contact
        mirrors/<source>.git                a bare mirror of each source repository (fetched by "Check GitHub")
        store/versions/<module>/<version>/<module>/   every version that was installed, kept for going back
        store/active/<module>               link to the version in use (this folder comes FIRST in addons_path)
        backups/<database>/                 a database dump before each update (and its filestore snapshot)
        runs/<database>/                    a journal of each rollback (kept outside the database it replaces)

Nothing here runs shell commands from user text: git, ssh-keygen, pg_* and cp get argument lists, tags, module and
database names are checked against strict patterns, and every path is built from those checked parts.
"""
import ast
import base64
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

from odoo.tools import config

_logger = logging.getLogger(__name__)

# when this process started: an update waits for a process that started after its code was switched
PROCESS_STARTED = time.time()

MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,6}$")
TAG_RE = re.compile(r"^(?P<module>[a-z][a-z0-9_]{1,80})-(?P<version>\d+(?:\.\d+){1,6})$")
# a package: one annotated tag on a batch commit (owner, 2026-10-26), e.g. release-tours-20261026-5
PACKAGE_RE = re.compile(r"^release-(?P<repo>[a-z0-9]{1,20})-(?P<date>\d{8})-(?P<n>\d{1,3})$")
DB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
GIT_TIMEOUT = 120
# an upgrade that changes a scheduled job the live server is running at that moment fails with this: it is tried
# again a few times
BUSY_CRON = "This cron task is currently being executed"
BUSY_TRIES = 5
BUSY_WAIT = 20
# an upgrade that waited too long for a lock (a live session held it): ended sessions, a longer lock wait, again
LOCK_ERRORS = ("lock timeout", "LockNotAvailable", "55P03", "could not obtain lock")
LOCK_TIMEOUTS = ("30s", "60s", "120s")
RELEASE_MARK = ".mtbg_release"  # in a version folder: the commit of the release tag it was extracted from


class StoreError(Exception):
    pass


def version_key(version):
    """Sortable form of a version: "19.0.0.2.10" > "19.0.0.2.9"."""
    return tuple(int(part) for part in (version or "0").split(".") if part.isdigit())


def root():
    """The server's update folder: ``mtbg_github_updates_dir`` in the server configuration, else
    <data_dir>/mtbg_github_updates. Two servers on one machine (sharing data_dir) need one each."""
    return config.get("mtbg_github_updates_dir") or os.path.join(config["data_dir"], "mtbg_github_updates")


def path(*parts):
    for part in parts:
        if not part or "/" in part or part in (".", ".."):
            raise StoreError("Unsafe path part: %r" % (part,))
    return os.path.join(root(), *parts)


def active_dir():
    return os.path.join(root(), "store", "active")


def check_db(dbname):
    if not DB_RE.match(dbname or "") or dbname.startswith("-"):
        raise StoreError("Unexpected database name: %s" % dbname)
    return dbname


def configured_addons_paths():
    """addons_path as written in the server configuration. Odoo leaves out a folder without modules when it starts
    (the store's active folder before the first update), so its own list cannot tell whether the store is set up."""
    import configparser
    rcfile = config.get("config")
    if rcfile and os.path.isfile(rcfile):
        parser = configparser.RawConfigParser()
        parser.read(rcfile)
        raw = parser.get("options", "addons_path", fallback="")
        return [os.path.realpath(os.path.expanduser(p.strip())) for p in raw.split(",") if p.strip()]
    import odoo.addons
    return [os.path.realpath(p) for p in odoo.addons.__path__]


def in_addons_path():
    """Is the store's active folder the FIRST of the configured addons paths, so that a version chosen here wins
    over every other copy (the old Tours and CFD stores, the code trees)?"""
    paths = configured_addons_paths()
    return bool(paths) and paths[0] == os.path.realpath(active_dir())


def shadowed_by(module):
    """The folder before the store's active folder in addons_path that has its own copy of ``module`` (Odoo would
    load that copy, not the version chosen here), or None."""
    paths = configured_addons_paths()
    active = os.path.realpath(active_dir())
    for folder in paths[:paths.index(active)] if active in paths else paths:
        if os.path.isfile(os.path.join(folder, module, "__manifest__.py")):
            return folder
    return None


def ensure_dirs():
    for sub in ("keys", "mirrors", os.path.join("store", "versions"), os.path.join("store", "active"), "backups", "runs"):
        os.makedirs(os.path.join(root(), sub), exist_ok=True)
    os.chmod(os.path.join(root(), "keys"), 0o700)


# ----------------------------------------------------------------------
# git
# ----------------------------------------------------------------------
def key_path(source_code):
    return path("keys", source_code)


def token_path(source_code):
    return path("keys", source_code + ".token")


def read_token(source_code):
    try:
        with open(token_path(source_code), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def save_token(source_code, token):
    """A read-only access token (https repositories); empty: removed. Kept in a file of its own (0600), never in
    the database or on a command line."""
    ensure_dirs()
    target = token_path(source_code)
    if not token:
        if os.path.exists(target):
            os.remove(target)
        return
    if not re.match(r"^[A-Za-z0-9_\-.]{10,255}$", token):
        raise StoreError("This does not look like an access token.")
    with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        handle.write(token)


def git_env(source_code, url):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if os.path.isdir(url):
        return env
    key = key_path(source_code)
    if os.path.exists(key):
        env["GIT_SSH_COMMAND"] = " ".join([
            "ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=%s" % path("known_hosts")])
    token = read_token(source_code)
    if token and url.startswith("https://"):
        # through the environment (not visible in the process list): git's config from env, git 2.31+
        basic = base64.b64encode(("x-access-token:%s" % token).encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0="Authorization: Basic %s" % basic)
    return env


def run_git(source_code, url, *args, git_dir=None):
    command = ["git"] + (["--git-dir", git_dir] if git_dir else []) + list(args)
    try:
        result = subprocess.run(command, env=git_env(source_code, url), capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as error:
        raise StoreError("git timed out: %s" % " ".join(args[:2])) from error
    if result.returncode:
        raise StoreError((result.stderr or result.stdout or "git failed").strip()[-2000:])
    return result.stdout


def mirror_path(source_code):
    return path("mirrors", source_code + ".git")


def fetch(source_code, url):
    """Create or refresh the bare mirror of ``url``; returns its path."""
    ensure_dirs()
    mirror = mirror_path(source_code)
    if os.path.isdir(mirror):
        run_git(source_code, url, "remote", "set-url", "origin", url, git_dir=mirror)
        run_git(source_code, url, "fetch", "--prune", "--prune-tags", "--tags", "origin",
                "+refs/heads/*:refs/heads/*", git_dir=mirror)
    else:
        run_git(source_code, url, "clone", "--mirror", "--quiet", url, mirror)
    return mirror


def list_tags(source_code, url):
    """[(tag, commit, date iso, subject, annotated)] of the mirror."""
    out = run_git(source_code, url, "for-each-ref", "refs/tags",
                  "--format=%(refname:short)%09%(*objectname)%09%(objectname)%09%(creatordate:iso8601-strict)"
                  "%09%(objecttype)%09%(contents:subject)",
                  git_dir=mirror_path(source_code))
    tags = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 6:
            tag, peeled, obj, date, kind, subject = parts[:6]
            tags.append((tag, peeled or obj, date, subject, kind == "tag"))
    return tags


def commits_between(source_code, url, since, upto):
    """The commits reachable from ``upto`` but not from ``since`` (None: ``upto`` alone), as a set."""
    for ref in (since, upto):
        if ref and not re.match(r"^[0-9a-f]{7,64}$", ref):
            raise StoreError("Not a commit: %s" % ref)
    if not since:
        return {upto}
    out = run_git(source_code, url, "rev-list", "%s..%s" % (since, upto), git_dir=mirror_path(source_code))
    return set(out.split())


def commit_subject(source_code, url, commit):
    if not re.match(r"^[0-9a-f]{7,64}$", commit or ""):
        return ""
    try:
        return run_git(source_code, url, "log", "-1", "--format=%s", commit, git_dir=mirror_path(source_code)).strip()
    except StoreError:
        return ""


def still_released(source_code, url, subdir, module, commit, cache=None):
    """Whether ``module`` (released at ``commit``) still exists in the source: in the tree of the newest branch head
    that contains that commit. A retired module (its folder removed, its old tags kept) is gone there. A commit on
    no branch (a tag pushed alone) counts as still released."""
    cache = {} if cache is None else cache
    if not commit or not MODULE_RE.match(module):
        return True
    mirror = mirror_path(source_code)
    if ("head", commit) not in cache:
        try:
            out = run_git(source_code, url, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)",
                          "--contains", commit, "refs/heads", git_dir=mirror)
        except StoreError:
            out = ""
        cache[("head", commit)] = (out.split() or [None])[0]
    head = cache[("head", commit)]
    if not head:
        return True
    inner = "%s/%s" % (subdir.strip("/"), module) if subdir else module
    key = ("tree", head, inner)
    if key not in cache:
        try:
            run_git(source_code, url, "cat-file", "-e", "%s:%s/__manifest__.py" % (head, inner), git_dir=mirror)
            cache[key] = True
        except StoreError:
            cache[key] = False
    return cache[key]


def tag_notes(source_code, url, tag):
    if not (TAG_RE.match(tag) or PACKAGE_RE.match(tag)):
        raise StoreError("Not a release tag: %s" % tag)
    return run_git(source_code, url, "tag", "-l", "--format=%(contents)", tag, git_dir=mirror_path(source_code)).strip()


def _show(source_code, url, subdir, module, tag, relpath):
    if not TAG_RE.match(tag) or not MODULE_RE.match(module):
        raise StoreError("Not a release tag: %s" % tag)
    inner = "%s/%s" % (subdir.strip("/"), module) if subdir else module
    return run_git(source_code, url, "show", "%s:%s/%s" % (tag, inner, relpath), git_dir=mirror_path(source_code))


def release_manifest(source_code, url, subdir, module, tag):
    """The manifest of ``module`` at release ``tag``, read from the mirror (nothing extracted)."""
    text = _show(source_code, url, subdir, module, tag, "__manifest__.py")
    try:
        manifest = ast.literal_eval(text)
    except (ValueError, SyntaxError) as error:
        raise StoreError("The manifest of %s is not readable: %s" % (tag, error)) from error
    if not isinstance(manifest, dict):
        raise StoreError("The manifest of %s is not readable." % tag)
    return manifest


def changelog_section(source_code, url, subdir, module, tag, version):
    """The CHANGELOG.md section of ``version`` at ``tag`` ("## <version> ..." up to the next "## "), or ""."""
    try:
        text = _show(source_code, url, subdir, module, tag, "CHANGELOG.md")
    except StoreError:
        return ""
    lines, keep = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if keep:
                break
            keep = line[3:].strip().split(" ")[0] == version
            continue
        if keep:
            lines.append(line)
    return "\n".join(lines).strip()


def remote_tag_count(source_code, url):
    out = run_git(source_code, url, "ls-remote", "--tags", "--refs", url)
    return len([line for line in out.splitlines() if line.strip()])


def generate_key(source_code, comment):
    ensure_dirs()
    key = key_path(source_code)
    for name in (key, key + ".pub"):
        if os.path.exists(name):
            os.remove(name)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", key],
                   check=True, capture_output=True, timeout=30)
    os.chmod(key, 0o600)
    return public_key(source_code)


def public_key(source_code):
    try:
        with open(key_path(source_code) + ".pub", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


# ----------------------------------------------------------------------
# versions on disk
# ----------------------------------------------------------------------
def version_dir(module, version):
    if not MODULE_RE.match(module) or not VERSION_RE.match(version):
        raise StoreError("Bad module or version: %s %s" % (module, version))
    return path("store", "versions", module, version)


def folder_manifest(folder):
    try:
        with open(os.path.join(folder, "__manifest__.py"), encoding="utf-8") as handle:
            manifest = ast.literal_eval(handle.read())
    except (OSError, ValueError, SyntaxError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def install_version(source_code, url, subdir, module, version, tag):
    """Extract ``module`` from ``tag`` into the store (once per release); returns the module folder. The tag's
    manifest must carry the tag's version. A folder of that version not extracted from this tag's commit (a build of
    unreleased work under the same number, an older extraction) is set aside under store/stale and the release
    extracted in its place."""
    if not TAG_RE.match(tag) or tag != "%s-%s" % (module, version):
        raise StoreError("Not the release tag of %s %s: %s" % (module, version, tag))
    target = os.path.join(version_dir(module, version), module)
    commit = run_git(source_code, url, "rev-parse", tag + "^{commit}", git_dir=mirror_path(source_code)).strip()
    mark = os.path.join(version_dir(module, version), RELEASE_MARK)
    if os.path.isfile(os.path.join(target, "__manifest__.py")):
        try:
            with open(mark) as f:
                if f.read().strip() == commit:
                    return target
        except OSError:
            pass
        stale = path("store", "stale", "%s-%s-%s" % (module, version, time.strftime("%Y%m%d%H%M%S")))
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        _logger.warning("Updates: %s %s in the store is not the release %s: set aside in %s",
                        module, version, commit[:10], stale)
        shutil.move(target, stale)
    inner = "%s/%s" % (subdir.strip("/"), module) if subdir else module
    ensure_dirs()
    with tempfile.TemporaryDirectory(dir=root()) as tmp:
        archive = os.path.join(tmp, "module.tar")
        run_git(source_code, url, "archive", "--format=tar", "-o", archive, tag, inner, git_dir=mirror_path(source_code))
        extract = os.path.join(tmp, "x")
        with tarfile.open(archive) as tar:
            tar.extractall(extract, filter="data")  # no absolute paths, no links out of the folder
        found = os.path.join(extract, inner)
        if not os.path.isfile(os.path.join(found, "__manifest__.py")):
            raise StoreError("%s is not in %s" % (module, tag))
        if folder_manifest(found).get("version") != version:
            raise StoreError("%s says version %s, not %s: not installed" % (tag, folder_manifest(found).get("version"),
                                                                            version))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(found, target)
    with open(mark, "w") as f:
        f.write(commit + "\n")
    return target


def adopt_version(module, version, folder, commit=""):
    """Copy a version another store runs (the Tours or CFD updater's) into this one, as it is: nothing changes in the
    code the server loads. Returns the module folder here. A copy already here with the same manifest is kept."""
    target = os.path.join(version_dir(module, version), module)
    if folder_manifest(folder).get("version") != version:
        raise StoreError("%s in %s is not version %s" % (module, folder, version))
    if os.path.isfile(os.path.join(target, "__manifest__.py")):
        return target
    ensure_dirs()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".adopting"
    if os.path.lexists(tmp):
        shutil.rmtree(tmp)
    shutil.copytree(folder, tmp, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    os.replace(tmp, target)
    if commit:
        with open(os.path.join(version_dir(module, version), RELEASE_MARK), "w") as f:
            f.write(commit + "\n")
    return target


def active_target(module):
    """The version folder ``module`` loads from the store, or None (another copy: an old store, the code tree)."""
    link = os.path.join(active_dir(), module)
    if os.path.islink(link):
        return os.path.realpath(link)
    return None


def active_version(module):
    target = active_target(module)
    if not target:
        return None
    return os.path.basename(os.path.dirname(target))


def active_modules():
    try:
        return sorted(name for name in os.listdir(active_dir()) if MODULE_RE.match(name)
                      and os.path.islink(os.path.join(active_dir(), name)))
    except OSError:
        return []


def switch(module, target):
    """Point the active link of ``module`` at ``target`` (a version folder), or remove it (None: the copy that comes
    after the store in addons_path). Atomic: the link is replaced in one step."""
    if not MODULE_RE.match(module):
        raise StoreError("Bad module: %s" % module)
    ensure_dirs()
    link = os.path.join(active_dir(), module)
    if target is None:
        if os.path.islink(link):
            os.remove(link)
        return
    real = os.path.realpath(target)
    if not real.startswith(os.path.realpath(path("store", "versions")) + os.sep):
        raise StoreError("Not a stored version: %s" % target)
    tmp = link + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(real, tmp)
    os.replace(tmp, link)


def has_migrations(folder, after, upto):
    """Whether the module in ``folder`` has migration scripts for a version in (after, upto]: going back over them
    without the database restore would leave data the older code does not expect."""
    base = os.path.join(folder or "", "migrations")
    try:
        names = os.listdir(base)
    except OSError:
        return False
    low, high = version_key(after), version_key(upto)
    for name in names:
        full = name if name.count(".") >= 4 else "19.0." + name  # "0.6.3" means 19.0.0.6.3
        if VERSION_RE.match(full) and low < version_key(full) <= high:
            return True
    return False


# ----------------------------------------------------------------------
# the upgrade, in a process of its own
# ----------------------------------------------------------------------
def upgrade_database(dbname, modules, timeout=3600, install=(), before_try=None):
    """Upgrade ``modules`` of ``dbname`` in a process of its own (``-u ... --stop-after-init``), with the code the
    store links to now, while this server keeps running the old code (a new stored field every request reads would
    otherwise break every request before an upgrade inside one could add its column). ``install``: new modules,
    installed in the same run (``-i``). ``before_try``: called before each run (maintenance: end the sessions that
    could hold a lock). A run that failed on a lock timeout is tried again with a longer lock wait. Returns (ok,
    what went wrong: the log's last error)."""
    import sys
    from odoo.tools.misc import stripped_sys_argv
    check_db(dbname)
    if not all(MODULE_RE.match(m) for m in [*modules, *install]):
        raise StoreError("Unexpected module name.")
    args = [a for a in stripped_sys_argv() if a not in ("--stop-after-init",)]
    if not args or os.path.basename(args[0]) == os.path.basename(sys.executable):
        args = args[1:]  # odoo-bin and its options, without the interpreter
    ensure_dirs()
    busy = lock = 0
    while True:
        if before_try:
            before_try()
        env = dict(os.environ)
        if lock:
            env["PGOPTIONS"] = ("%s -c lock_timeout=%s" % (env.get("PGOPTIONS", ""), LOCK_TIMEOUTS[lock - 1])).strip()
        # its own log file: the error comes back here whatever the server's logging (a log file, syslog)
        with tempfile.NamedTemporaryFile("r", dir=root(), prefix="upgrade-", suffix=".log") as log:
            command = [sys.executable, *args, "-d", dbname,
                       *(["-i", ",".join(install)] if install else []), *(["-u", ",".join(modules)] if modules else []),
                       "--stop-after-init", "--no-http", "--max-cron-threads=0", "--log-level=info",
                       "--logfile=%s" % ("" if config.get("syslog") else log.name)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=env)
            output = "\n".join(part for part in (log.read(), result.stdout or "", result.stderr or "") if part).strip()
        if result.returncode == 0:
            return True, ""
        if BUSY_CRON in output and busy < BUSY_TRIES - 1:
            busy += 1
            time.sleep(BUSY_WAIT)
            continue
        if any(error in output for error in LOCK_ERRORS) and lock < len(LOCK_TIMEOUTS):
            lock += 1
            _logger.warning("Updates: the upgrade of %s waited too long for a lock; again with lock_timeout=%s",
                            dbname, LOCK_TIMEOUTS[lock - 1])
            continue
        return False, error_tail(output) or ("exit code %s, no log" % result.returncode)


def error_tail(output, size=4000):
    """What an upgrade's log says went wrong: from its last error (the log line, then the traceback), else its
    last lines."""
    traceback = output.rfind("Traceback (most recent call last)")
    if traceback >= 0:
        line = output.rfind("\n", 0, traceback) + 1
        start = output.rfind("\n", 0, max(line - 1, 0)) + 1  # the error's log line, just above
    else:
        error = max(output.rfind(" ERROR "), output.rfind(" CRITICAL "))
        start = output.rfind("\n", 0, error) + 1 if error >= 0 else 0
    return output[start:][-size:]


# ----------------------------------------------------------------------
# backups, and the rollback's restore
# ----------------------------------------------------------------------
def _pg(tool, *args, timeout=3600, dbname=None):
    from odoo.tools.misc import exec_pg_environ, find_pg_tool
    result = subprocess.run([find_pg_tool(tool), *args], env=exec_pg_environ(), capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise StoreError("%s failed: %s" % (tool, (result.stderr or "error").strip()[-1000:]))
    return result.stdout


def filestore_dir(dbname):
    return os.path.join(config["data_dir"], "filestore", check_db(dbname))


def backup_database(dbname, label):
    """A pg_dump (custom format) of ``dbname`` and a hard-link snapshot of its filestore (cheap: the files are not
    copied; Odoo never changes a stored file, it writes new ones). Returns (dump path, snapshot path or "": the
    filestore on another disk than the store, or no filestore)."""
    check_db(dbname)
    if not re.match(r"^[a-z0-9-]{1,40}$", label):
        raise StoreError("Bad backup label.")
    folder = path("backups", dbname)
    os.makedirs(folder, exist_ok=True)
    os.chmod(folder, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = os.path.join(folder, "%s-%s-%s.dump" % (dbname, stamp, label))
    _pg("pg_dump", "--format=custom", "--no-owner", "--file", target, dbname)
    os.chmod(target, 0o600)
    snapshot = ""
    source = filestore_dir(dbname)
    if os.path.isdir(source):
        snapshot = os.path.join(folder, "%s-%s-%s.files" % (dbname, stamp, label))
        result = subprocess.run(["cp", "-al", "--", source, snapshot], capture_output=True, text=True, timeout=1800,
                                check=False)
        if result.returncode:
            _logger.warning("Updates: no filestore snapshot of %s (%s)", dbname, (result.stderr or "").strip()[-300:])
            shutil.rmtree(snapshot, ignore_errors=True)
            snapshot = ""
    return target, snapshot


def free_bytes(folder=None):
    try:
        stat = os.statvfs(folder or root())
    except OSError:
        return None
    return stat.f_bavail * stat.f_frsize


def restore_database(dump, newname):
    """``dump`` into a new database ``newname`` (owned by this server's role), the way Odoo restores one: an empty
    database with its extensions, then pg_restore, which must end without an error."""
    from odoo.service import db as odoo_db
    check_db(newname)
    if not os.path.isfile(dump) or not os.path.realpath(dump).startswith(os.path.realpath(path("backups")) + os.sep):
        raise StoreError("Not a backup of this store: %s" % dump)
    odoo_db._create_empty_database(newname)
    try:
        _pg("pg_restore", "--no-owner", "--dbname=" + newname, dump)
    except StoreError:
        drop_database(newname)
        raise


def drop_database(dbname):
    from contextlib import closing

    from odoo import sql_db
    from odoo.tools.sql import SQL
    check_db(dbname)
    sql_db.close_db(dbname)
    with closing(sql_db.db_connect("postgres").cursor()) as cr:
        cr._cnx.autocommit = True
        cr.execute(SQL("DROP DATABASE IF EXISTS %s", SQL.identifier(dbname)))


def database_rights(dbname):
    """What the rollback's restore needs of this server's role: create databases, own ``dbname``. Returns
    (ok, problem)."""
    from contextlib import closing

    from odoo import sql_db
    check_db(dbname)
    with closing(sql_db.db_connect("postgres").cursor()) as cr:
        cr.execute("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user")
        createdb = (cr.fetchone() or [False])[0]
        cr.execute("""SELECT pg_get_userbyid(datdba) = current_user OR (SELECT rolsuper FROM pg_roles
                       WHERE rolname = current_user) FROM pg_database WHERE datname = %s""", (dbname,))
        owner = (cr.fetchone() or [False])[0]
    if not createdb:
        return False, "this server's database user may not create databases (CREATEDB)"
    if not owner:
        return False, "this server's database user does not own the database %s" % dbname
    return True, ""


def swap_databases(dbname, restored, keep_as):
    """``dbname`` becomes ``keep_as`` and ``restored`` becomes ``dbname``: two renames, with every session of
    ``dbname`` ended first (a rename needs none). Raises when a session of another role is still connected."""
    from contextlib import closing

    from odoo import sql_db
    from odoo.modules.registry import Registry
    from odoo.tools.sql import SQL
    for name in (dbname, restored, keep_as):
        check_db(name)
    Registry.delete(dbname)
    sql_db.close_db(dbname)
    sql_db.close_db(restored)  # this process's own pooled connections (the restore, the history written into it)
    with closing(sql_db.db_connect("postgres").cursor()) as cr:
        cr._cnx.autocommit = True
        cr.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                       WHERE datname IN (%s, %s) AND pid <> pg_backend_pid()""", (dbname, restored))
        cr.execute("SELECT count(*), string_agg(DISTINCT usename, ', ') FROM pg_stat_activity WHERE datname = %s",
                   (dbname,))
        count, users = cr.fetchone()
        if count:
            raise StoreError("%s session(s) of %s (%s) are still connected: the database cannot be swapped"
                             % (count, dbname, users))
        cr.execute(SQL("ALTER DATABASE %s RENAME TO %s", SQL.identifier(dbname), SQL.identifier(keep_as)))
        try:
            cr.execute(SQL("ALTER DATABASE %s RENAME TO %s", SQL.identifier(restored), SQL.identifier(dbname)))
        except Exception:
            cr.execute(SQL("ALTER DATABASE %s RENAME TO %s", SQL.identifier(keep_as), SQL.identifier(dbname)))
            raise


def swap_filestore(dbname, snapshot, keep_as):
    """The filestore of ``dbname`` set aside as ``keep_as``'s, the snapshot's copy (hard links again) in its place.
    No snapshot: the current files stay (a restored database may then miss files cleaned since its backup)."""
    current = filestore_dir(dbname)
    if not snapshot or not os.path.isdir(snapshot):
        return False
    aside = filestore_dir(keep_as)
    if os.path.isdir(current):
        shutil.move(current, aside)
    result = subprocess.run(["cp", "-al", "--", snapshot, current], capture_output=True, text=True, timeout=1800,
                            check=False)
    if result.returncode:
        if os.path.isdir(aside) and not os.path.exists(current):
            shutil.move(aside, current)
        raise StoreError("The files could not be restored: %s" % (result.stderr or "").strip()[-300:])
    return True
