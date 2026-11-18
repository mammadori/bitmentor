# Adding a language

bitmentor is written in English and carries Italian and German. A community adds any other language from its own
folder, without changing the software.

## 1. The pages and the list

1. Write the pages in `pages/<language>/` of the community folder; `home.md` is required, the others can come later.
2. Add the language to `languages` in `community.yml`, with the code Django uses (`fr`, `es`, `sl`, `pt`, `pt-br`…) and
   the name to show in the language switcher:

   ```yaml
   languages:
     en: English
     fr: Français
   ```

   The first language is the default.
3. Add the language to the texts of `community.yml` that have one entry per language (the menu `labels`, `tagline`
   and the others): a text without that language uses the default language.

From here the site answers at `/<language>/`, and visitors who reach `/` with the browser set to that language are
sent there. A page missing in a language returns 404, is left out of the sitemap and of the `hreflang` links, and the
language switcher leads to that language's home page.

Dates, numbers and amounts follow the language on their own, on the server and in the browser.

## 2. The software's texts

The panel, the register, the map and the emails take their texts from a gettext catalog. Without one for your language
they are shown in English, next to the few texts that come from Django (form errors, month names), which Django
already has in most languages.

1. Copy `locale/django.pot` of the software to `locale/<language>/LC_MESSAGES/django.po` in the **community** folder.
   The folder takes the locale name: `fr`, `sl`, `pt_BR` for `pt-br`.
2. Set `Language` and `Plural-Forms` at the top to those of your language (Slovenian, for example, has four plural
   forms and needs `msgstr[0]` to `msgstr[3]` where the template has two).
3. Fill in the `msgstr` lines. Keep the placeholders as they are (`%(name)s`, `{name}`, `%%`) and the HTML tags.
   A text left empty is shown in English.
4. Compile the catalog: the site reads only the `.mo` file.

   ```bash
   msgfmt -o locale/fr/LC_MESSAGES/django.mo locale/fr/LC_MESSAGES/django.po
   ```

   `manage.py compilemessages` with `COMMUNITY_DIR` set does the same for every language. With images, compile before
   the build ([Advanced installation](installation.md)).

The community's catalog comes before the software's: the same way a community can change a text of the Italian or
German translation, by putting only that text in its own `locale/it/` or `locale/de/`.

## After each release

A release can add texts or change them, and a changed text is a new one for gettext: until it is translated it is
shown in English. Compare the new `locale/django.pot` with your catalog (`msgmerge --update` does it and marks what
changed), translate, compile again.

The under-construction page (`maintenance/index.html`) is a static file with its own words in English, Italian and
German: a community that builds its own Caddy image replaces it there ([Advanced installation](installation.md)).
