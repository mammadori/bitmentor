"""Which of the site's languages a native speaker has read, for the mark in the language switcher.

A catalog says it of itself, in a header of its own (`X-Native-Reviewed`): absent, empty or `no` it has not been read;
anything else it has, and the value says what was read (a version, a date). The community's catalog of a language
speaks before the software's, and `locale/django.pot` speaks for English, the language the texts are written in.
`native_reviewed` in community.yml has the last word: `true` for every language, or one value for each language.
A language nobody says anything about gets no mark: the software cannot know."""

import re
from functools import lru_cache

from django.conf import settings
from django.utils.translation import to_locale

from .community import community

HEADER = "X-Native-Reviewed"
ENTRY = re.compile(r'^"(.*)"\s*$')


def header(path, name=HEADER):
    """The value of a header of a .po or .pot file, None when the file or the header is missing."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.split('msgstr ""', 1)[-1].splitlines()[1:]
    for line in lines:
        found = ENTRY.match(line)
        if not found:
            break
        key, _, value = found[1].removesuffix("\\n").partition(":")
        if key.strip().lower() == name.lower():
            return value.strip()
    return None


@lru_cache
def declared(code):
    """What the catalogs say of a language: True, False, or None when none of them says."""
    paths = [folder / to_locale(code) / "LC_MESSAGES" / "django.po" for folder in settings.LOCALE_PATHS]
    if code == "en":
        paths.append(settings.LOCALE_PATHS[-1] / "django.pot")
    for path in paths:
        value = header(path)
        if value is not None:
            return value.lower() not in ("", "no", "false")
    return None


def reviewed(code):
    """Whether to show `code` as read by a native speaker; a language nobody speaks for counts as read."""
    chosen = community().get("native_reviewed")
    if chosen is True:
        return True
    if isinstance(chosen, dict) and isinstance(chosen.get(code), bool):
        return chosen[code]
    return declared(code) is not False


def marked(code):
    """The mark goes on the languages a visitor may choose, never on the community's own, the first one."""
    return code != settings.LANGUAGE_CODE and not reviewed(code)
