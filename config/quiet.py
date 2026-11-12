"""What opens something is never written whole in the logs of `web`.

A secret travels in the query wherever it can, because access logs write the path (shops/staff.py, shops/moving.py).
Team access (team/, docs/features/team-access.md) has two in a path: the token of an invite's link,
`/team/join/<token>/`, and the challenge a sign-in page polls, `/team/login/<k1>/` (the k1 of an invite's page is worth
the invite until it is signed; that page never asks for such an address, but the cut does not rely on it). Here both
are cut from what gunicorn's access log writes (`AccessLog`, named in config/gunicorn.py) and from what Django and
the site's own code write to the console (`QuietPaths`, a filter of the console handler in config/settings.py: Django
logs the path of every answer from 400 up). Caddy's logs cut them with the same expression (Caddyfile).

The cut does not ask for the path to be a real one: capitals, doubled slashes, a prefix before /team/ (a language's,
`/en/team/join/…`) and percent-encoded letters are cut too, since a mistyped or mangled link carries the same token."""
import logging
import re
from urllib.parse import unquote

# What follows /team/join/ or /team/login/, up to the next slash: kept in step with the two expressions of the Caddyfile
SECRET_PATH = re.compile(r"(?i)(/+team/+(?:join|login)/+)[^/?\s]+")
# A challenge in a query (LNURL-auth's callback), wherever a whole address is written
SECRET_QUERY = re.compile(r"(?i)([?&]k1=)[^&\s]+")


def cut(text):
    return SECRET_QUERY.sub(r"\1-", SECRET_PATH.sub(r"\1-", text))


def quiet(text):
    """The text with the secret part of every such path replaced by a dash. A text that hides one behind percent-encoding
    (`/team/%6Aoin/…`, which the site reads as the same path) is written decoded, and cut."""
    done = cut(text)
    if "%" in done:
        decoded = unquote(done)
        if cut(decoded) != decoded:
            return cut(decoded)
    return done


class QuietPaths(logging.Filter):
    """Rewrites a record before the handler formats it: the message and its string arguments."""

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = quiet(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(quiet(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {name: quiet(arg) if isinstance(arg, str) else arg for name, arg in record.args.items()}
        return True


def access_log_class():
    """gunicorn's logger with quiet paths. Made on demand: gunicorn is not imported by the site's own code."""
    from gunicorn.glogging import Logger

    class Quiet(Logger):
        def atoms(self, resp, req, environ, request_time):
            # Every atom, not only the path the format writes today: the request line, the query, the referer and
            # the variables of the request carry the same path
            atoms = super().atoms(resp, req, environ, request_time)
            return {name: quiet(value) if isinstance(value, str) else value for name, value in atoms.items()}

    return Quiet


def __getattr__(name):
    # `logger_class = "config.quiet.AccessLog"` in config/gunicorn.py: gunicorn asks for the name when it starts
    if name == "AccessLog":
        return access_log_class()
    raise AttributeError(name)
