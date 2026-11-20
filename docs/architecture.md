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
  text without that language uses English where the site has it, then the default one. Visitors who reach `/` with
  the browser set to a listed language go to `/<lang>/`: its home page must exist before the language is listed. A language the software does not carry:
  [Adding a language](languages.md).
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

This is the list of the ways a community's code meets the software. "Since" is the first release that has it.

| Way | Where | Since |
|---|---|---|
| [Hooks](#hooks): the software asks | `COMMUNITY_HOOKS`, `config/hooks.py` | 0.0.1 |
| [Events](#events): the software tells | `shops/events.py` | 0.2.0 |
| [Template blocks](#template-blocks): a page extended, not copied | the community's `templates/` folder | 0.2.0 |
| [Settings](#settings-of-the-communitys-settings-module) the community's settings module may set | its settings module | see the table |
| [The `python/` folder](#the-python-folder) of `COMMUNITY_DIR` | `config/settings.py` | 0.1.4 |
| [Copies of templates](#copies-of-the-softwares-templates) | the community's `templates/` folder | 0.0.1 |

### Hooks

Where the software must ask the community a question, it calls a function of the module named by `COMMUNITY_HOOKS`
(`config/hooks.py`). Every function is optional (all four since 0.0.1):

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

### Events

Where something happened that a community may want to know, the software sends a Django signal of `shops/events.py`
(from 0.2.0). The community connects its receivers in the `ready()` of one of its apps:

```python
# mycommunity/guestbook/apps.py
from django.apps import AppConfig


class Guestbook(AppConfig):
    name = "mycommunity.guestbook"

    def ready(self):
        from django.dispatch import receiver
        from shops import events

        @receiver(events.shop_created, weak=False)
        def new_shop(sender, shop, user, source, **kwargs):
            ...
```

The arguments are passed by name, and the names stay; a receiver takes `**kwargs` too, so that an argument added later
breaks nothing. The sender is the `Shop` class (`None` for `theme_file_served`). No event carries an email, an address
of the connection or a request: a receiver reads from the model what it needs.

| Event | Arguments | When it is sent |
|---|---|---|
| `shop_created` | `shop`, `user`, `source` | A shop comes to exist, once. `source`: `site` (the owner's form, /shops/new/), `admin` (written in /admin/), `import` (`manage.py import_shops`: one for each new shop of the list), `move` (`manage.py import_moved_shops`: a shop that was not here; one that takes the place of an unclaimed shop of the list is not new) or `demo` (`manage.py demo_data`). `user`: the account that did it (site, admin), else `None` |
| `shop_claimed` | `shop`, `user`, `via` | A shop that waited without an owner gets one. `via`: `claim_link` (the one-time link, `/shops/claim/`, made for shops moved from another site) or `move` (`manage.py import_moved_shops` puts a moved shop in the place of an unclaimed shop of the list and gives it to the account made for its owner's email). `shop` is the row as the database has it after the change. A moved shop that is new here comes with `shop_created`, source `move` (with an owner or not), and no `shop_claimed`. An owner given to an unclaimed shop in /admin/ sends no event |
| `shop_approved` | `shop`, `by` | A waiting shop is approved (`/admin/today/`, the action of /admin/, the volunteers' area: `approval.approve_or_hold`). `by` is what `approved_by` says: a username or `[team key N] name`. Never for a shop held for a check, nor for one somebody approved in the meantime; never for the shops approved as they are brought (imports, moves, the demo, a shop written in /admin/) nor for new shops when the community turned the approval off: those come with `shop_created` |
| `lightning_address_broken` | `shop` | The Lightning guardian decides that a shop's address stopped working (after its failed probes some time apart): once for each change of the status, not at every probe. Also during the shop's holiday, when its people are not told |
| `lightning_address_working` | `shop` | The guardian sees working again the same address it had found broken (also during a holiday). Only then: when the owner replaces a broken address the status starts again with no `working`, and a shop no longer watched (suspended, historic, without an address) sends nothing. A receiver that keeps a list of broken shops also ends a shop's state on `lightning_address_changed` and when the shop is no longer watched |
| `lightning_address_changed` | `shop`, `before`, `after` | A shop's Lightning address changes, from wherever the guardian of the accounts sees it (`shops/guard.py`: a `save()`, an `update()` or a `bulk_update()`, from the panel, a command or a shell), the first connection of a wallet too (`before=""`). Not for a new shop, nor for the made-up shops of the demo. `before` and `after` are the two addresses, `""` for none: do not write them whole in logs or messages (the software's own notices cut the name, `guard.short_address`) |
| `theme_file_served` | `path`, `first_byte`, `client_kind` | A file of the theme is being sent, only one that matches `announced_files` in `community.yml` (shell patterns relative to `theme/`, as `embeddable`: `[media/*.zip]`, up to 50 of at most 200 characters; without the key it is never sent, and a value it cannot use is a warning of `manage.py check`, `shops.W014`). `path`: relative to `theme/`. `first_byte`: `True` when the answer starts at the beginning of the file (no `Range`, or one from byte 0), so a download read in pieces usually counts once. Only roughly: Safari and iOS ask `bytes=0-1` before the file, and a player that seeks back to the start asks from 0 again; a receiver that needs exact counts cannot rely on it. `client_kind`: `browser` when the request carries `Sec-Fetch-Mode`, which every browser of today sends and programs as a rule do not, and its User-Agent names no robot or tool; else `program`. Not for a `HEAD`, a `304` or a refused `Range` |

All but `theme_file_served` are sent after the transaction commits (`transaction.on_commit`; at once when there is
none): a change rolled back sends nothing. `theme_file_served` is sent before the answer leaves. A receiver that raises
is written in the log (`shops.events`, with its name and the exception's class) and stops nothing: not the page, not the
command, not the other receivers. Receivers run in the request or the command that made the change: a slow one makes
them wait, so a receiver that talks to another server does it in a thread or a queue of its own. With no receivers an
event costs nothing.

### Template blocks

A community extends a page instead of copying it: a template with the same name in its `templates/` folder extends the
software's own (Django skips the template it is in and finds the next one with that name) and fills only the blocks it
changes; the rest of the page follows the software's changes by itself. Without a community that extends them, the
pages are byte for byte what they were. From 0.2.0:

```django
{# COMMUNITY_DIR/templates/shops/claim.html #}
{% extends "shops/claim.html" %}{% load i18n %}
{% block claim_gone %}<p class="notice" role="alert">{% translate "This link does not work: ask at our desk." %}</p>{% endblock %}
```

| Template | Blocks |
|---|---|
| `shops/claim.html` (the one-time link of a moved shop) | `claim_busy` (too many tries), `claim_gone` (a link used or not complete), `claim_form` (the confirmation), and inside it `claim_taken` (the address already belongs to another owner) |
| `shops/register_locked.html` (the register waiting for the owner's training) | `locked_owner` (what the owner reads), `locked_staff` (a staff register), `locked_others` (anybody else with the link) |

The other error pages of the register and of the panel have no blocks: `shops/staff_gone.html` (a staff register
turned off) is one sentence, and the panel has no error page of its own (the site's `404.html`).

### Settings of the community's settings module

A settings module of the community may set these, read by the software from Django's settings (most of the others come
from the environment, `.env`, and are documented in [Configuration](configuration.md)):

| Setting | What it does | Since |
|---|---|---|
| `COMMUNITY_HOOKS` | The module of the [hooks](#hooks) | 0.0.1 |
| `EMAIL_REAL_BACKEND` | The mail backend that really sends, behind the one that counts the emails ([A mail backend of the community](configuration.md#a-mail-backend-of-the-community)); also from the environment since 0.1.4 | 0.0.1 |
| `MERCHANT_FROM_EMAIL` | The sender of the emails to a shop's owner; also from the environment | 0.1.4 |
| `MAIL_IN_BACKGROUND` | `False`: the sign-in links, the wallet notices and the owner's notice of an approval leave before the page answers, instead of in a thread after it | 0.1.8 |
| `PUBLIC_LIST_SECONDS` | How long the public list of shops is kept before it is built again (60) | 0.0.1 |
| `INSTALLED_APPS`, `ROOT_URLCONF` | Django's own: the community's apps, and its addresses before the software's (above) | 0.0.1 |

**The log.** Whatever a community's code writes with `logging` at WARNING or above reaches the log of `web` with no line
of its own in `LOGGING` (from 0.2.0; before, only errors did, unless the community's settings gave
its loggers a level). The libraries are kept at their errors (`QUIET_LOGGERS` in `config/settings.py`).

### The `python/` folder

When `COMMUNITY_DIR` has a `python/` folder, `config/settings.py` puts it at the end of Python's path (since 0.1.4):
the community's modules there can be imported (a mail backend, a hooks module), and a module of the software with the
same name is still the software's. It is not an app: it is not in `INSTALLED_APPS` unless the settings module adds it.

### Copies of the software's templates

The community's `templates/` folder comes before the software's: a template there with the name of one of the
software's replaces it whole (since 0.0.1).
A community that keeps its own copies of the software's templates follows their changes. From 0.1.9 a shop is historic
only from the day after its closing day: copies of `shops/panel.html`, `shops/map_list.html` and `shops/shop.html` must
test `shop.closed` instead of `shop.closed_on`; copies of `shops/map.html` take the new `data-closes` text and
`data-today="{% now 'Y-m-d' %}"` (without it the map uses the phone's date). `shop.register_until` is now always empty
(a historic shop's register has no end).

## Nostr profile

`manage.py publish_nostr` signs the community profile along with the shops: `name`, `tagline` in the default language,
and a picture.

```yaml
nostr:
  picture: /theme/logo.png
```

`picture` is a path on the site or a full URL. Without it, the theme `logo` is used if it is PNG, JPEG, WebP or GIF; an
SVG logo is not, because many clients do not show it. Without either, the profile has no picture.
