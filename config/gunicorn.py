"""Gunicorn behind Caddy. Processes and threads are changed from .env (WEB_CONCURRENCY, WEB_THREADS), without rebuilding the image."""
import os

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
accesslog = "-"
access_log_format = '%(t)s "%(m)s %(U)s" %(s)s %(L)s'
# The path is written without what opens something in it (the token of a team invite): config/quiet.py
logger_class = "config.quiet.AccessLog"
