"""Restarting the server must start the same Python, with the same packages.

Odoo's restart (``odoo.service.server._reexec``) starts the new process with only the interpreter's name
(``python``) as its first argument. When the server runs from a virtual environment whose ``python`` is a link
to the system's (venvs made by uv, or ``python -m venv --copies`` off), the new process looks ``python`` up on
PATH, finds the system interpreter, loses the environment's packages and dies at the first import (seen as
``cannot import name 'etree' from partially initialized module 'lxml'``). Updates restart the server, so they
would leave it down. The same function with the full path of the running interpreter fixes it.

With workers, the restart runs in the main (prefork) process, which loads this module only when it opens a
database at start (``db_name``) or loads it as a server-wide module; otherwise only Odoo's own function runs
there. ``restart_problem`` tells, before an update, whether that restart would start the wrong Python.
"""
import logging
import os
import shutil
import signal
import sys

from odoo.service import server
from odoo.tools import config
from odoo.tools.misc import stripped_sys_argv

_logger = logging.getLogger(__name__)


def _reexec(updated_modules=None):
    args = stripped_sys_argv()
    if updated_modules:
        args += ["-u", ",".join(updated_modules)]
    if not args or os.path.basename(args[0]) != os.path.basename(sys.executable):
        args.insert(0, sys.executable)  # the full path: the same interpreter, the same environment
    if os.name == "posix":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    _logger.info("Restarting with %s", sys.executable)
    os.execve(sys.executable, args, os.environ)


def restart_problem(env):
    """Why the restart after an update would leave the server down (it would start another Python than this one,
    without its packages), with how to fix the server; None when it is safe."""
    if os.name != "posix" or not config.get("workers"):
        return None  # one process: it restarts itself with the function above
    wide = config.get("server_wide_modules") or []
    if isinstance(wide, str):
        wide = wide.split(",")
    if config.get("db_name") or "mtbg_github_updates" in [m.strip() for m in wide]:
        return None  # the main process loads this module, so it restarts with the function above
    name = os.path.basename(sys.executable)
    found = shutil.which(name)
    if found and os.path.dirname(os.path.abspath(found)) == os.path.dirname(os.path.abspath(sys.executable)):
        return None  # Odoo's own restart finds this Python on PATH
    return env._(
        "Updating now would leave the server down: after an update the server restarts, and this one (with workers, "
        "no db_name) would start %(found)s instead of %(exe)s, without this server's packages. Fix the server first "
        "(any one of these), restart it, then update:\n"
        "- put %(bin)s first on the PATH of the server's service (e.g. a systemd drop-in Environment=PATH=...);\n"
        "- or add mtbg_github_updates to server_wide_modules in the configuration;\n"
        "- or set db_name in the configuration.",
        found=found or env._("no %s on PATH", name), exe=sys.executable, bin=os.path.dirname(sys.executable))


# the Tours or CFD updater's copy of this fix does the same: either one is enough
if os.name == "posix" and not any(getattr(server._reexec, mark, False) for mark in ("_mtbg_patched", "_cfd_patched")):
    _reexec._mtbg_patched = True
    server._reexec = _reexec
