# Architecture

The software is kept separate from the community that uses it.

| Part | Where | Contents |
|---|---|---|
| Software | this repository | Code, services, shared features, base theme |
| Community | folder given by `COMMUNITY_DIR` | Name, contacts, logo, colors, styles, texts, history, languages |

A community is a folder, also kept in a private repository of its own:

```text
community.yml    name, contacts, channels, languages, features turned off, limits
theme/           colors, styles, logo, images
pages/<lang>/    Markdown pages
locale/          texts that replace the software's own
```

The secrets, the network and the domain name are not in the community folder: they are in the `.env` of each
installation, never in a repository ([Configuration](configuration.md)).

Rules:

- No community names, colors or texts are written in the code.
- Changing the look or the texts requires neither touching the code nor rebuilding the images.
- The community's features are switched on or off from `community.yml`; how the machine runs (network, mode, email)
  is in the `.env`.
- A `theme/sw.js` is optional: the site serves it with `Service-Worker-Allowed: /`, so a page can register it on its
  own path and stay readable offline.
- The site sends a Content-Security-Policy: no inline scripts in templates, only files. The exceptions are
  `<script type="speculationrules">` and `<script type="application/ld+json">`, which are data for search engines and
  are not executed. A page that needs to talk to another server declares it in its metadata, with
  `connect: https://server.example`: it applies to that page only.
- No page can be shown in another site's frame (`X-Frame-Options: DENY`, `frame-ancestors 'none'`), except the community's
  public pages listed in `embeddable` in `community.yml` (shell patterns such as `game-*`): those may be framed by every
  site, or only by the https origins in `embed_from`. Private and staff pages, the panel and the registers stay closed
  whatever the list says.
- Pages and data are served with `Cross-Origin-Resource-Policy: same-origin`: another site cannot load them as resources. `/static/`
  and `/theme/` stay open to everyone, because the theme logo is also the Nostr profile picture.
- The site does not accept files: Caddy accepts bodies up to 1 MiB, and Django discards files sent with a form.
- Colors are in `theme/theme.css`. `--on-primary` and `--on-highlight` are the text on top of `--primary` and
  `--highlight`: they are needed for a dark theme, or for a light color.
- The languages are those in `languages`, as many as you like; the first one is the default. The software's texts are
  in `locale/<lang>/` (the sources are in English). A page missing in a language returns 404 and is left out of the
  sitemap and of the `hreflang` links, and the language switcher leads to that language's home page; a `community.yml`
  text without that language uses the default one. Visitors who reach `/` with the browser set to a listed language go
  to `/<lang>/`: its home page must exist before the language is listed.
- Pages go through the template engine: `{{ community.name }}` and `{{ tagline }}` follow `community.yml`, and
  `{% include "website/route.html" %}` puts stops and map where they are needed; the community's meetings have their
  own board ([Meetups](features/meetups.md)).
- The metadata at the top of a page: `title` and `description` go into the page head and into the preview shown when
  someone shares the link; `image` picks the preview image instead of `share_image` from `community.yml`;
  `layout: wide` widens the page; `connect` is described above.
- `examples/community/` is a neutral and complete example, to copy and rewrite: a test checks that the project works
  with it, in every language.

## The community's own code

Most communities need only their folder: `community.yml`, pages, theme. A community that needs code of its own (a
page with its own data, a rule only it knows) adds it **beside** the software, never inside it:

- its code is a Python package of Django apps, in the community's image or on `PYTHONPATH`;
- a settings module of its own starts from the software's and adds to it, and the site runs with it
  (`DJANGO_SETTINGS_MODULE`, which `manage.py` and `config/wsgi.py` only set when it is missing; `docker-compose.yml`
  passes neither it nor `PYTHONPATH` to `web`: set both as `ENV` in the community's image):

  ```python
  # mycommunity/settings.py
  from config.settings import *  # noqa: F401, F403

  INSTALLED_APPS += ["mycommunity.guestbook"]
  ROOT_URLCONF = "mycommunity.urls"
  COMMUNITY_HOOKS = "mycommunity.hooks"        # below
  ```

  ```python
  # mycommunity/urls.py: its own addresses first, then the software's, in the same list. Not include("config.urls"):
  # the software's addresses carry the language (/en/…), which Django allows only in the root list
  from django.urls import path

  from config.urls import urlpatterns as software
  from mycommunity.guestbook import views

  urlpatterns = [path("guestbook/", views.page), *software]
  ```

Where the software must ask the community a question, it calls a function of the module named by `COMMUNITY_HOOKS`
(`config/hooks.py`). Every function is optional:

| Function | What it answers | Used by |
|---|---|---|
| `training_grace_until(user)` | A `date`: the last day on which this owner's real register opens without the basic pass. `None`: no days of their own | [The merchant's training](features/merchant-training.md): the register stays open until that day ends, and the panel says how many days are left |
| `training_practice_done(user)` | `True` if this owner already practised at a register the site cannot see | The practice step of the training counts as done |
| `private_ok(user)` | `True` if this account sees the private part of the pages | [Public and private parts of a page](features/private-pages.md): asked only when the software's own rule says no |
| `dashboard_items()` | A list of lines: `{"what": …, "do": …}`, one line of plain text each (cut at 300 characters), and if needed `level` (`problem`, `warning`, `info`, the default), `code` (letters, digits and `_`: it names the line, and the morning message tells a line once by its name; without it the text is the name) and `links` (a list of `{"text", "url"}`, an address of the site or `https://`) | [What to do today](features/dashboard.md) |

It is a setting, not a key of `community.yml`: only whoever writes the installation's settings can point the site at
code. A function that raises, or answers something of another kind (a text where a date is asked, `1` where `True` is),
is written in the log and counts as no answer: a mistake there never breaks a page and never opens the private part,
and a failing `training_grace_until` gives no days of its own, so the register waits for the pass as it would without
it. The one exception is a database that does not answer inside that function: the register stays open, as whenever
the database is away. A function may be asked more than once for a page: it should answer fast, and the same.
`manage.py check` warns (`shops.W012`) when the setting is not a module's name, when the module cannot be imported,
or when one of these names is not a function.

## Nostr profile

`manage.py publish_nostr` signs the community profile along with the shops: `name`, `tagline` in the default language,
and a picture.

```yaml
nostr:
  picture: /theme/logo.png
```

`picture` is a path on the site or a full URL. Without it, the theme `logo` is used if it is PNG, JPEG, WebP or GIF; an
SVG logo is not, because many clients do not show it. Without either, the profile has no picture.
