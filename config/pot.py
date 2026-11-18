"""locale/django.pot: the software's texts with no translation, the starting point for a new language.

The catalogs are written by hand, so the template is made from the Italian one, which a test keeps complete:
`python -m config.pot` writes it again after a text is added, changed or removed."""
from pathlib import Path

LOCALE = Path(__file__).resolve().parent.parent / "locale"
SOURCE = LOCALE / "it" / "LC_MESSAGES" / "django.po"
TEMPLATE = LOCALE / "django.pot"
HEAD = """\
# The texts of bitmentor, to translate into a new language: see docs/languages.md.
# A text left empty is shown in English. Set Language and Plural-Forms below to those of your language.
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Language: \\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"
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


if __name__ == "__main__":
    TEMPLATE.write_text(template(SOURCE.read_text()))
