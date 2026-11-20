#!/usr/bin/env python3
"""Puts aside the old files of a community's theme, for the Blossom server to keep serving them for a while
(docs/features/blossom.md, «Retiring the files»). bitmentor itself copies nothing: this is the tool that does it.

Run it on the machine, after the community folder was updated, saying where the files were before:

  blossom-retire.py <community folder> --git <commit>     the folder is a git checkout: <commit> is where it was
                                                          before the update (ORIG_HEAD after a pull, HEAD@{1}, a tag)
  blossom-retire.py <community folder> --from <folder>    an older copy of the community folder (a backup, the
                                                          previous release)
  blossom-retire.py <community folder> --prune <days>     only removes what was put aside more than <days> ago

Every public file of the old `theme/` whose bytes are in no file of the theme now is copied to
`<community folder>/blossom-retired/`, under the same path. --prune can be added to the other two; it goes by the date
of the files, which is when they were put aside: give it at least the days of `blossom_retire_days`, and `touch` a
file put in the folder by hand. Needs only Python 3.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

FOLDER = "blossom-retired"
# The same list as website/blossom.py (a test compares them)
EXTENSIONS = frozenset(
    (
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".avif",
        ".svg",
        ".ico",
        ".pdf",
        ".epub",
        ".mp3",
        ".ogg",
        ".opus",
        ".m4a",
        ".wav",
        ".flac",
        ".mp4",
        ".webm",
        ".mov",
        ".zip",
        ".gz",
        ".tar",
        ".tgz",
        ".woff2",
    )
)


def public(name):
    parts = name.split("/")
    return not any(part.startswith(".") for part in parts) and os.path.splitext(parts[-1])[1].lower() in EXTENSIONS


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_of(base):
    """relative path -> file, for the public files of a folder; links are left out."""
    found = {}
    for folder, dirs, files in os.walk(base):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(folder, name))]
        for name in files:
            path = Path(folder, name)
            relative = path.relative_to(base).as_posix()
            if public(relative) and not path.is_symlink() and path.is_file():
                found[relative] = path
    return found


def put_aside(retired, relative, digest, source, move=False):
    """Puts an old file under its path; if another old version has that path, the hash's start goes in the name. The
    file is new, so its date is now: --prune counts from here."""
    target = retired / relative
    if target.exists() and sha256_of(target) != digest:
        target = target.with_name(f"{target.stem}.{digest[:8]}{target.suffix}")
    if target.exists():
        if move:
            source.unlink()
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        os.replace(source, target)
    else:
        temporary = target.with_name(f".{target.name}.part")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    os.utime(target)
    return True


def from_folder(community, old, now):
    count = 0
    for relative, path in sorted(files_of(old / "theme").items()):
        digest = sha256_of(path)
        if digest not in now:
            count += put_aside(community / FOLDER, relative, digest, path)
    return count


def from_git(community, commit, now):
    if commit.startswith("-"):
        sys.exit(f"not a commit: {commit}")
    git = ["git", "-C", str(community)]
    # Run in the community folder, git names the files from there, also when the folder is inside a larger checkout
    listed = subprocess.run(
        [*git, "ls-tree", "-r", "-z", commit, "--", "theme"], check=True, capture_output=True
    ).stdout
    retired = community / FOLDER
    count = 0
    for line in filter(None, listed.decode(errors="surrogateescape").split("\0")):
        meta, name = line.split("\t", 1)
        mode, kind, blob = meta.split()
        relative = name.removeprefix("theme/")
        if kind != "blob" or mode == "120000" or not public(relative):
            continue
        # The old bytes go to a file as they are read: a large video never sits in memory
        retired.mkdir(exist_ok=True)
        temporary = retired / f".{blob}.part"
        digest = hashlib.sha256()
        with (
            subprocess.Popen([*git, "cat-file", "blob", blob], stdout=subprocess.PIPE) as process,
            open(temporary, "wb") as stream,
        ):
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                digest.update(chunk)
                stream.write(chunk)
        if process.returncode:
            temporary.unlink()
            sys.exit(f"git could not read {name}")
        if digest.hexdigest() in now:
            temporary.unlink()
        else:
            count += put_aside(retired, relative, digest.hexdigest(), temporary, move=True)
    return count


def prune(community, days):
    """Removes what was put aside long ago, with the unfinished copies of a run that was stopped and the empty folders."""
    retired = community / FOLDER
    limit = time.time() - days * 86400
    count = 0
    for path in files_of(retired).values():
        if path.stat().st_mtime < limit:
            path.unlink()
            count += 1
    for folder, _dirs, files in os.walk(retired, topdown=False):
        for name in files:
            path = Path(folder, name)
            if name.startswith(".") and name.endswith(".part") and path.stat().st_mtime < time.time() - 86400:
                path.unlink()
        if Path(folder) != retired and not os.listdir(folder):
            os.rmdir(folder)
    return count


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("community", type=Path)
    parser.add_argument("--git", metavar="COMMIT")
    parser.add_argument("--from", dest="old", metavar="FOLDER", type=Path)
    parser.add_argument("--prune", metavar="DAYS", type=int)
    options = parser.parse_args(arguments)
    if not (options.community / "theme").is_dir():
        parser.error(f"{options.community} has no theme/ folder")
    if (options.git and options.old) or not (options.git or options.old or options.prune is not None):
        parser.error("say --git <commit> or --from <folder>, and/or --prune <days>")
    if options.prune is not None and options.prune < 1:
        parser.error("--prune wants one day or more")
    if options.git or options.old:
        now = {sha256_of(path) for path in files_of(options.community / "theme").values()}
        if options.git:
            count = from_git(options.community, options.git, now)
        else:
            count = from_folder(options.community, options.old, now)
        print(f"{count} old file(s) put aside in {options.community / FOLDER}")
    if options.prune is not None:
        print(
            f"{prune(options.community, options.prune)} file(s) removed, put aside more than {options.prune} days ago"
        )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(f"git: {error.stderr.decode().strip()}")
