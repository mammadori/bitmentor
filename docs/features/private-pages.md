# Public and private parts of a page

A page can keep a part for friends and say it with decorum to everyone else. The marks stand alone on their lines,
with an empty line before and after:

```
::: private
Text for friends: paragraphs, lists, tables, headings.
:::

::: public
The same thing said in public.
:::
```

Whoever has the private view reads the `private` blocks and not the `public` ones; everyone else the opposite. Outside
the blocks everything is public. Blocks do not nest and do not sit inside a list or a quote; they can sit inside HTML,
with or without `markdown="1"`, because the rule works on the raw text, before the template and Markdown. A footnote
used only in a private block is defined inside it; the site drops anyway every definition nobody calls, with all its
paragraphs, and then the notes only those called. Title, description and preview image are always public. A page with
`visibility: private` in its metadata is private as a whole: a 404 for everyone else, never in the sitemap or the
menu, and never a link from `community.yml` (menu, `shop_page`). A `visibility` the software does not
understand makes the page private too, and `manage.py check` says so (`website.W005`). A page with `visibility: staff`
is a 404 for everyone but staff and admin accounts and the allowed addresses: shop owners, certified or not, never see
it. Like a private page, it stays out of the menu, the sitemap and the static copy.

Every line that starts with `:::` is a mark, written exactly as above, or an error: indented, in capitals, another
word, or inside a fenced code block. A block never closed, one inside another, or a wrong mark make the page answer
404, and `manage.py check` says where (`website.W003`). Windows line ends and a byte order mark are fine. See
`examples/community/pages/*/stories.md`.

Who has the private view is decided by the server at every request:

- whoever comes from an address in `DEV_ALLOWED_IPS` to the site's address, in every `SITE_MODE`. Caddy always writes
  `X-Forwarded-For` itself, with the address it trusts from `TRUSTED_PROXIES`, and `X-Entrance` (`site`, or `onion` on
  the onion's entrance); Django compares the address with the list only when the entrance is `site`. What a visitor
  sends in those headers is replaced. Another site block of `caddy.d` that sends to `web` must write
  `request_header X-Entrance onion` too. `manage.py check` warns (`website.W004`) about `private_ranges`, internal or
  loopback networks (except `127.0.0.1` and `::1`) and networks wider than /24 or /48;
- with `TEAM_ACCESS=on`, on a site under construction, a browser with a valid team pass: it counts as an allowed
  address ([Team access with a Lightning wallet](team-access.md));
- the owner of a certified shop that a person of the association approved by hand in `/admin/` (`shops/certified.py`,
  `sees_private_part`; see [Certified shops](certified-shops.md)): the badge alone gives nothing;
- an account the community's own code lets in (`private_ok`,
  [The community's own code](../architecture.md#the-communitys-own-code)), asked only when the two above say no.

They see a small line at the top of every page with the view they are in (from an allowed address also the
`SITE_MODE`) and a button to hide or show the private part. The choice is remembered with the `private_view` cookie,
which on its own gives nothing. The static copy of the site, asked with `X-Static-Copy: 1`, is always public, even from
an allowed address. Every page, and every answer for whoever may have the private view, carries
`Cache-Control: private, no-cache` and `Vary: Cookie`. A page answered to someone signed in, and the sign-in page and
the pages of the email links, add `no-store`: not even the browser keeps them on disk, for whoever comes next to a shared
computer.
