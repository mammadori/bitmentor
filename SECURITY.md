# Security

bitmentor shows customers where to pay a shop: a flaw in it can send money to the wrong place. If you find one, tell
us before telling everybody.

## How to report

Use GitHub's private report, **Security → Report a vulnerability**, on
[the repository](https://github.com/mammadori/bitmentor/security/advisories/new). Only the maintainers see it. Please
do not open a public issue for a flaw that can be used against a site.

Write what you found, how to reproduce it (a version or a commit, the steps, what you expected and what happened), and
what someone could do with it. A guess is welcome too: say that it is one.

## What to expect

bitmentor is kept on a best-effort basis by its maintainers ([No warranty](README.md#no-warranty-no-legal-advice)).
We answer as soon as we can, fix what is confirmed, and name you in the fix if you like. There is no bounty.

## What is in scope

- The software in this repository: the site, the register in the browser, the scripts in `deploy/`.
- Not the site of a community that runs bitmentor: its configuration, its server and its data are that community's.
  Tell the community; if the cause is in the software, tell us too.

What the software does not defend against, by design, is in [Limitations](docs/limitations.md): read it first.
