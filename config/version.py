"""The version this site runs, for the footer of every page.

The site's own version if the community's image sets one, otherwise the software's. With no exact version (run from
the sources, or an image built without a release tag) the git commit, with «-dirty» when tracked files have changes
that are not committed; «no-version» when there is no git either.
"""

import subprocess
from functools import cache
from pathlib import Path

from django.conf import settings

NO_VERSION = "no-version"
# What the Dockerfile puts in BITMENTOR_VERSION when the image is not built from a release tag
UNTAGGED = "development"


def _git(*arguments):
    try:
        done = subprocess.run(
            ["git", "-C", str(settings.BASE_DIR), *arguments], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def commit():
    """The short commit of the sources, «-dirty» with uncommitted changes; empty outside a git checkout."""
    top = _git("rev-parse", "--show-toplevel")
    # A checkout of something else that happens to contain the sources says nothing about them
    if not top or Path(top).resolve() != Path(settings.BASE_DIR).resolve():
        return ""
    short = _git("rev-parse", "--short", "HEAD")
    if not short:
        return ""
    changed = _git("status", "--porcelain", "--untracked-files=no")
    return f"{short}-dirty" if changed else short


@cache
def in_use(site, software):
    if site:
        return site
    if software and software != UNTAGGED:
        return software
    return commit() or NO_VERSION


def context(request):
    """`version_in_use` for the templates: empty when the site hides it (SHOW_VERSION=0)."""
    if not settings.SHOW_VERSION:
        return {"version_in_use": ""}
    return {"version_in_use": in_use(settings.SITE_VERSION, settings.BITMENTOR_VERSION)}
