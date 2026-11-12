"""The community's own code, called where the software asks a question only the community can answer.

A community that needs more than `community.yml` runs the site with a settings module of its own, which imports
`config.settings` and adds its apps (docs/architecture.md, "The community's own code"). There it can name one module,
`COMMUNITY_HOOKS = "package.module"`, with any of these functions; each is optional, and without the setting nothing here
does anything:

- `training_grace_until(user) -> date | None`: the last day on which the real register of this owner opens without the
  basic pass of the training (shops/training.py). None: no days of its own, the community's `training` block decides.
- `training_practice_done(user) -> bool`: this owner already practised at a register, somewhere the site cannot see:
  the practice step of the training counts as done.
- `private_ok(user) -> bool`: this account sees the private part of the pages (website/visibility.py), asked only when
  the software's own rule said no.
- `dashboard_items() -> list`: more lines for the association's list of the day (shops/dashboard.py): dictionaries
  with `what` and `do` (texts), and optionally `level` ("problem", "warning" or "info", the default), `code` and
  `links` (`{"text", "url"}`).

It is a setting, not a key of `community.yml`: only whoever writes the installation's settings can point the site at
code. A function that raises, or answers something of another kind, is written in the log and counts as no answer: a
mistake in the community's code never breaks a page and never opens the private part, and a failing
`training_grace_until` gives no days of its own (the community's `training` block decides, as without it). One
exception: when that function cannot reach the database, the register stays open, as it does whenever the database
does not answer (shops/training.py, `locked`). A function may be asked more than once for a page: it should answer
fast, and the same."""
import logging
import time
from datetime import date, datetime
from importlib import import_module

from django.conf import settings

logger = logging.getLogger(__name__)
NAMES = ("training_grace_until", "training_practice_done", "private_ok", "dashboard_items")
# A module that could not be imported is tried again after this long: a failure is never kept for the life of a worker
# (one worker of three with no hooks would answer differently from the others), and the log is not filled at every request
RETRY_SECONDS = 60
_loaded = {}
_failed = {}


def module():
    """The module named by COMMUNITY_HOOKS, or None: without the setting, or when it cannot be imported (`manage.py
    check` says so, shops.W012). Kept by name once imported, so a setting that changes (the tests) is followed."""
    name = getattr(settings, "COMMUNITY_HOOKS", "")
    if not name or not isinstance(name, str):
        return None
    if name in _loaded:
        return _loaded[name]
    if time.monotonic() - _failed.get(name, -RETRY_SECONDS) < RETRY_SECONDS:
        return None
    try:
        found = import_module(name)
    except Exception:  # noqa: BLE001 (the community's code: whatever it raises, the site still starts)
        logger.exception("COMMUNITY_HOOKS: %s cannot be imported; no hook is called", name)
        _failed[name] = time.monotonic()
        return None
    _loaded[name] = found
    return found


def forget():
    """Forgets what was imported and what failed: for the tests, which change the module behind one name."""
    _loaded.clear()
    _failed.clear()


module.cache_clear = forget


def function_of(name):
    """The community's function `name`, or None: also for a module that raises when asked for it."""
    try:
        found = getattr(module(), name, None)
    except Exception:  # noqa: BLE001 (a module-level __getattr__ of the community's)
        logger.exception("COMMUNITY_HOOKS: %s cannot be read; it counts as missing", name)
        return None
    return found if callable(found) else None


def defined(name):
    """Whether the community answers this question: callers that must fetch something first ask before they do."""
    return function_of(name) is not None


def call(name, *args, passes=()):
    """The answer of the community's function `name`, or None: without the function, or when it raises. An exception of
    the kinds in `passes` is not swallowed: the caller has a rule of its own for it."""
    function = function_of(name)
    if function is None:
        return None
    try:
        return function(*args)
    except passes:
        raise
    except Exception:  # noqa: BLE001 (the community's code: a mistake there is no answer, never an error of the page)
        logger.exception("COMMUNITY_HOOKS: %s raised; it counts as no answer", name)
        return None


def yes(name, *args):
    """True only when the function answers exactly True."""
    answer = call(name, *args)
    if answer is not None and not isinstance(answer, bool):
        logger.error("COMMUNITY_HOOKS: %s answered %r, not True or False; it counts as no", name, answer)
    return answer is True


def day(name, *args, passes=()):
    """The day the function answers, or None: for anything that is not a date (a date with a time is not a day)."""
    answer = call(name, *args, passes=passes)
    if answer is None:
        return None
    if isinstance(answer, datetime) or not isinstance(answer, date):
        logger.error("COMMUNITY_HOOKS: %s answered %r, not a date; it counts as no answer", name, answer)
        return None
    return answer


def rows(name, *args):
    """The dictionaries the function answers, as a list; what is not a dictionary is left out."""
    answer = call(name, *args)
    if answer is None:
        return []
    if not isinstance(answer, (list, tuple)):
        logger.error("COMMUNITY_HOOKS: %s answered %r, not a list; it counts as no answer", name, answer)
        return []
    return [row for row in answer if isinstance(row, dict)]


def problems():
    """What is wrong with the setting, for `manage.py check`."""
    name = getattr(settings, "COMMUNITY_HOOKS", "")
    if not name:
        return []
    if not isinstance(name, str):
        return [f"COMMUNITY_HOOKS is the dotted name of a module, like \"mycommunity.hooks\": {name!r}; no hook is called"]
    try:
        found = import_module(name)
    except Exception as error:  # noqa: BLE001 (said to whoever runs the check, with the reason)
        return [f"COMMUNITY_HOOKS: the module {name} cannot be imported ({type(error).__name__}: {error}); no hook is called"]
    return [f"COMMUNITY_HOOKS: {name}.{attribute} is not a function; it is never called"
            for attribute in NAMES if hasattr(found, attribute) and not callable(getattr(found, attribute))]
