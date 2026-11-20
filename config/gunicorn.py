"""Gunicorn behind Caddy. Processes and threads are changed from .env (WEB_CONCURRENCY, WEB_THREADS), without rebuilding the image."""

import os
import subprocess
import sys
from pathlib import Path

bind = "0.0.0.0:8000"
worker_class = "gthread"
# A process uses at most one CPU. On a 4-CPU machine three leave room for Postgres and Caddy.
workers = int(os.environ.get("WEB_CONCURRENCY") or 3)
# Threads wait for Telegram, the database or a slow client without stopping the process
threads = int(os.environ.get("WEB_THREADS") or 8)
# Caddy keeps connections open for 60 seconds: gunicorn keeps them a bit longer, so it does not close the very one
# Caddy is sending a request on
keepalive = 75
# With threads `timeout` does not stop a long request: it only checks that the process is alive. Waits on outside services
# each have their own limit: Telegram and mail 10 seconds, Lightning addresses 10.
timeout = 30
graceful_timeout = 30
# gunicorn's control socket (gunicornc, since 25.1) is on unless told otherwise: nobody uses it here, and a socket
# that takes commands is one thing less to keep an eye on
control_socket_disable = True
accesslog = "-"
access_log_format = '%(t)s "%(m)s %(U)s" %(s)s %(L)s'
# The path is written without what opens something in it (the token of a team invite): config/quiet.py
logger_class = "config.quiet.AccessLog"


def when_ready(server):
    """Once per start of the site, in the master, not in each worker: the settings watch (website/settings_watch.py) in
    a process of its own, which tells the moderators of a security setting changed since the previous start. The start
    waits for nothing, and nothing that goes wrong there stops it; gunicorn's master collects the process when it ends."""
    try:
        subprocess.Popen(
            [sys.executable, "manage.py", "settings_watch", "compare"],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        server.log.warning("settings watch: it could not start (%s)", type(error).__name__)
