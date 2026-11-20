"""locale/django.pot: the software's texts with no translation, the starting point for a new language.

The catalogs are written by hand, so the template is made from the Italian one, which a test keeps complete:
`python -m config.pot` writes it again after a text is added, changed or removed.
`python -m config.pot --since 0.1.9` lists the texts that changed since a version, for the notes of a release: a
community with a catalog of its own, or with pages that quote a button, learns from them what to look at."""

import re
import subprocess
import sys
from pathlib import Path

LOCALE = Path(__file__).resolve().parent.parent / "locale"
SOURCE = LOCALE / "it" / "LC_MESSAGES" / "django.po"
TEMPLATE = LOCALE / "django.pot"
HEAD = """\
# The texts of bitmentor, to translate into a new language: see docs/languages.md.
# A text left empty is shown in English. Set Language and Plural-Forms below to those of your language.
# X-Native-Reviewed: `no` until a native speaker has read the catalog, then what was read (a version, a date); the
# language switcher marks the languages nobody has read (docs/languages.md). Here it speaks for the English texts.
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Language: \\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"
"X-Native-Reviewed: no\\n"
"""


def template(catalog):
    """The text of a .po with every translation emptied; of the comments only the format flags (`#,`) stay."""
    lines, translation = [], False
    for line in catalog.split("\n\n", 1)[1].splitlines():
        if translation and line.startswith('"'):
            continue
        translation = line.startswith("msgstr")
        if translation:
            lines.append(f'{line.split(" ", 1)[0]} ""')
        elif not line.startswith("#") or line.startswith("#,"):
            lines.append(line)
    return f"{HEAD}\n" + "\n".join(lines) + "\n"


def ids(catalog):
    """The msgids of a catalog or of the template, in its order: the lines of one written on several joined."""
    found = []
    for match in re.finditer(r'^msgid ((?:".*"\n?)+)', catalog, re.MULTILINE):
        msgid = "".join(re.findall(r'"(.*)"', match[1]))
        if msgid:
            found.append(msgid)
    return found


def changes(before, after):
    """(added, removed): the msgids only `after` has and those only `before` has. A text that was reworded is one of
    each: a community that translated it, or quotes it in a page, finds it among the removed."""
    old, new = set(ids(before)), set(ids(after))
    return [msgid for msgid in ids(after) if msgid not in old], [msgid for msgid in ids(before) if msgid not in new]


def since(ref):
    """The lines for a release's notes: what changed in the texts since the version `ref` (a tag such as 0.1.9)."""
    try:
        shown = subprocess.run(
            ["git", "show", f"{ref}:locale/django.pot"], cwd=LOCALE.parent, capture_output=True, text=True, check=False
        )
    except OSError:  # no git on this machine (the site's image has none): the comparison is for a clone
        return ["git is not here: run this in a clone of the software, nothing to compare with"]
    if shown.returncode:
        return [f"{ref} has no locale/django.pot (the template exists from 0.1.9): nothing to compare with"]
    added, removed = changes(shown.stdout, template(SOURCE.read_text()))
    if not (added or removed):
        return [f"No text changed since {ref}."]
    lines = [f"Texts since {ref}: {len(added)} new, {len(removed)} gone (a reworded text is one of each)."]
    lines += [f"  gone: {msgid}" for msgid in removed] + [f"  new: {msgid}" for msgid in added]
    return lines


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--since":
        print("\n".join(since(sys.argv[2])))
    elif len(sys.argv) == 1:
        TEMPLATE.write_text(template(SOURCE.read_text()))
    else:
        sys.exit(
            "python -m config.pot            writes locale/django.pot again\n"
            "python -m config.pot --since <tag>   the texts that changed since that version, for the release notes"
        )
