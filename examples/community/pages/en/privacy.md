---
title: Privacy
description: What data this site handles, why, for how long, and your rights.
---
# Privacy

> **TEMPLATE, NOT LEGAL ADVICE.** This text is a starting point supplied with the software. It describes what the software
> does with its default settings. Before you publish it: replace every `[PLACEHOLDER]`, delete what does not apply to the
> features you use, check it against the law of your own country (the GDPR applies in the EU/EEA and to sites aimed at
> people there), and remove this notice. The software is provided "as is", without warranty; its developers accept no
> liability, and each community is responsible for its own site and compliance. The guide for whoever installs the
> site is `docs/privacy.md` in the software.

## Who we are

The controller of the data on this site is **[CONTROLLER NAME]**, [LEGAL FORM AND ADDRESS], in **[COUNTRY]**.
Contact for anything about your data: **[CONTACT]**, or [{{ community.email }}](mailto:{{ community.email }}).
[If you have one: Data protection officer: DPO CONTACT.]

## What we handle, why and for how long

**Visitors.** No profiling, no advertising, no third-party statistics and no third-party scripts. The site sets only
technical cookies (the session after sign-in, the form protection, and for the staff a cookie that remembers their device).
The server keeps technical logs of requests for security and diagnosis: [LOGS: for example "the network of the
address (not the whole address), the time, the page and the result, kept at most 7 days"; write what your installation
really does].

**Maps.** When you open a map, your browser connects to the servers of OpenStreetMap, which see your IP address and
browser details. [If you changed this: say how.]

**Merchants and other people who sign in.** With a Lightning wallet (LNURL-auth) we keep a public key that the wallet
made for this site alone. If you give an email, we use it to let you back in and to send you notices about your shop.
We keep your account as long as you use it; [ACCOUNT RETENTION: for example "we delete accounts unused for 3 years"].
You can ask us to delete it at any time.

**Shops.** The data of a shop (name, description, address, position, phone, email, website, business name and VAT
number, Lightning address) is kept while the shop is on the site. A shop's page is public by the owner's choice; phone,
email, address, business name and VAT number are shown only if the owner ticks "show", and the owner can hide or remove
the shop whenever they want. Public shop data can also be published on Nostr relays (see below).

**Requests for help.** If a merchant asks for help, we keep the line they wrote, what the site knew about the shop and the
account. {{ community.privacy.help_requests_days|default:365 }} days after the request was solved, the personal part is
deleted. The request is also sent to our moderators' Telegram chat [if used]; messages there are deleted after
[TELEGRAM RETENTION].

**Payments.** The register asks the merchant's Lightning address for each invoice, from the merchant's browser, and checks
it with the provider of their wallet; the exchange rate comes from public sources ([PRICE SOURCES]). [ONLY IF YOU
TURNED ON-CHAIN ON, OTHERWISE REMOVE: On-chain payments are checked with a public explorer ([EXPLORER]).] These
connections do not pass through us: the site sees neither amounts
nor invoices, and the sales stay on the merchant's phone. The money goes straight to the merchant's wallet. We never hold
funds or keys.

**Emails.** We send emails (sign-in links, security notices) through [MAIL PROVIDER]. We keep, for 62 days, the time and a
coded print of the address, only to stay inside our sending limits.

**Backups.** Every night we make an encrypted copy of the site's data and keep it at [BACKUP DESTINATION]. Copies are kept
for [BACKUP RETENTION: with the default settings, the last 30 days and the first of each month for 12 months]. If we
delete your data, it disappears from the backups when they expire, and not before.

**Nostr.** [If you use it.] Public shop profiles can be published on Nostr relays, and a register may keep an
encrypted copy there. Relays belong to other people: **what is published there cannot be erased by us**.

## Why we may process your data

[LEGAL BASES: for example "to give you the service you asked for (contract); because you agreed, for what is published
(consent); and for the security of the service (legitimate interest)". Match them to docs/privacy.md and to your country.]

## Who receives your data

[HOSTING PROVIDER] (hosting, in [COUNTRY / REGION]), [MAIL PROVIDER] (email), [TELEGRAM, if used], [BACKUP DESTINATION],
[NOSTR RELAYS, if used], and OpenStreetMap for the maps. [Transfers outside the EU/EEA and the safeguard used, for example
the standard contractual clauses.] We do not sell data.

## Your rights

You can ask us for access to your data, its correction, its deletion, a restriction of its use, a copy of it, and you can
object to its use. If you gave your consent you can withdraw it at any time. Write to **[CONTACT]**: we answer within
one month. You can also complain to the supervisory authority: **[COUNTRY / SUPERVISORY AUTHORITY, with address or
website]**.

## Changes

[DATE of the last change.] We will say here if this text changes.
