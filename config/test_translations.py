"""The .po files are written by hand: one reader of them, and one test that every string of every template is in each
language of locale/, with the same placeholders. A string added without them shows in English to everyone else."""
import re

from django.conf import settings
from django.test import SimpleTestCase

LOCALE = settings.BASE_DIR / "locale"
TEMPLATES = settings.BASE_DIR / "templates"
PLACEHOLDER = r"%\(\w+\)[sd]|\{\w+\}"


def languages():
    return sorted(path.parent.parent.name for path in LOCALE.glob("*/LC_MESSAGES/django.po"))


def catalogue(language):
    """{msgid: msgstr} of locale/<language>/LC_MESSAGES/django.po (the singular forms)."""
    text = (LOCALE / language / "LC_MESSAGES" / "django.po").read_text()
    found = {}
    for match in re.finditer(r'^msgid ((?:".*"\n)+)msgstr ((?:".*"\n?)+)', text, re.M):
        msgid, msgstr = ("".join(re.findall(r'"(.*)"', group)).replace('\\"', '"') for group in match.groups())
        found[msgid] = msgstr
    return found


def template_ids(paths):
    """The msgids of the {% translate %} and {% blocktranslate %} (without plural) of the templates."""
    found = set()
    for path in paths:
        text = path.read_text()
        for match in re.finditer(r"""\{%\s*translate\s+(?:"([^"]*)"|'([^']*)')""", text):
            # makemessages writes a % of the template as %%, as it does in a blocktranslate
            found.add((match.group(1) if match.group(1) is not None else match.group(2)).replace("%", "%%"))
        for match in re.finditer(r"\{%\s*blocktranslate\b([^%]*)%\}(.*?)\{%\s*endblocktranslate\s*%\}", text, re.S):
            options, body = match.groups()
            if "{% plural %}" in body:
                continue
            if "trimmed" in options:
                body = " ".join(line.strip() for line in body.strip().splitlines() if line.strip())
            found.add(re.sub(r"\{\{\s*(\w+)[^}]*\}\}", r"%(\1)s", body.replace("%", "%%")))
    return found


def python_ids(paths):
    """The msgids of _("...") and gettext_lazy("...") in the Python files, adjacent strings joined."""
    found = set()
    for path in paths:
        for match in re.finditer(r"""(?<![\w.])(?:_|gettext_lazy|gettext)\(\s*((?:"(?:[^"\\]|\\.)*"\s*)+)\)""", path.read_text()):
            found.add("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))).replace('\\"', '"'))
    return found


def untranslated(test, ids, exempt=()):
    """Asserts that every msgid is translated, with the same placeholders, in every language of locale/."""
    test.assertTrue(languages())
    catalogues = {language: catalogue(language) for language in languages()}
    missing = {language: sorted(msgid for msgid in ids - set(exempt) if not found.get(msgid))
               for language, found in catalogues.items()}
    test.assertEqual({language: found for language, found in missing.items() if found}, {})
    for language, found in catalogues.items():
        wrong = sorted(msgid for msgid in ids - set(exempt)
                       if sorted(re.findall(PLACEHOLDER, found[msgid])) != sorted(re.findall(PLACEHOLDER, msgid)))
        test.assertEqual(wrong, [], f"{language}: placeholders")


class Translations(SimpleTestCase):
    def test_every_string_of_every_template_is_in_every_language(self):
        ids = template_ids(sorted(TEMPLATES.rglob("*.html")))
        self.assertGreater(len(ids), 500)
        # Django's own words, translated by Django
        untranslated(self, ids, exempt={"Home"})
