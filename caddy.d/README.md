The `*.caddy` files in this folder apply to the site. They are for the local rules of one installation.
Who sees the site is not decided here: `SITE_MODE` decides it, with the rules in `caddy/`. The `sites/*.caddy` files are
imported as separate site blocks, for other sites served by the same Caddy.
