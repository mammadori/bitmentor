# Blossom: the site's files by their hash

With `blossom: true` in `community.yml` and `BLOSSOM_HOSTS` in the `.env`, bitmentor also answers as a read-only
[Blossom](https://github.com/hzrd149/blossom) server (BUD-01) for the community's public files. Anyone can ask for a
file by its SHA-256, check that the bytes match the hash, and copy it to another Blossom server, so the files outlive
this one. There is no upload, no authorization and no new service: it is a view over the files the site already serves.
Off by default; with `blossom` off, every address below is a 404 with `X-Reason: blossom is off`.

| Address, on a Blossom host | What it does |
|---|---|
| `GET`/`HEAD /<sha256>` and `/<sha256>.<ext>` | The file, with its `Content-Type` and `Content-Length`, `Access-Control-Allow-Origin: *`, `Cache-Control: public, max-age=31536000, immutable` (the content of a hash never changes) and `Range`. `OPTIONS` answers the CORS preflight. A hash the site does not have is a 404. The extension is a hint only: the hash decides |
| `GET /list` | JSON `{"server": …, "files": [{"sha256", "size", "type", "url", "blossom"}]}`: for each file, its hash, size, type, the address on the site (`/theme/…`) and the Blossom one |
| `PUT`, `DELETE`, `/upload`, `/mirror`, `/media`, `/report` | 403 with `X-Reason: read-only server` |

**The hosts.** BUD-01 wants every endpoint at the root of the domain, so that any client can use any server; the root
of the site belongs to the pages. So the Blossom server has hosts of its own, and the site has no Blossom address under
a path. `BLOSSOM_HOSTS=blossom.example.org` makes that host only the Blossom server: any other address there is refused
with its reason. With more names, separated by commas (`BLOSSOM_HOSTS=blossom.example.org,files.example.net`), each
answers the same; the first is the one `/list` gives as the server and in the files' addresses. Each name needs a DNS
record to the machine and a site in Caddy, in `caddy.d/sites/blossom.caddy` (Caddy takes the certificates):

```
blossom.example.org, files.example.net {
	import anonymous
	encode zstd gzip
	import to-web
}
```

**Errors and CORS.** Every answer, errors included, has `Access-Control-Allow-Origin: *`, so a client on another site
reads the error too. Errors say why in `X-Reason`, as BUD-01 asks: 400 for a name that is not a SHA-256 with an
optional extension, 404 for a hash the site does not serve, 403 for writing. The preflight (`OPTIONS`) allows
`GET, HEAD, PUT, DELETE, OPTIONS` and the headers `Authorization, *`: a client that tries to upload gets to ask, and
reads the 403 with its reason.

**The list.** `/list` is not BUD-02's `/list/<pubkey>`: these files are nobody's uploads and nobody signs them,
and BUD-12 advises against listing by key. It is the site's own index, for whoever wants to copy everything elsewhere.

**Counting the users.** To know whether people use it, the requests are counted without personal data
(`website/blossom_visits.py`). A user is the visitor's network (an IPv4 /24, an IPv6 /48), kept only
as an HMAC whose key comes from `SECRET_KEY` and changes every month; one row a day per user, deleted after 90 days.
Not counted: the addresses of `DEV_ALLOWED_IPS`, private and local networks, bots, and whoever only asked for the list.
`/admin/today/` shows the different users of the last 30 days (within one month's key, so nobody counts twice). The
first time they reach `blossom_alert_users` of `community.yml` (3 when unset, 0 never), the moderators are told, the way
the morning summary goes.

## Retiring the files

A hash is for ever, a disk is not. When the community replaces a file of the theme, its old hash is a 404 at once, and
whoever linked it finds nothing. Keeping every old version would fill the disk of whoever hosts the site, so this is
off unless the community asks, and bitmentor itself copies nothing and writes no file: the community decides what is
kept, and for how long.

```yaml
blossom_retire_days: 30
blossom_mirrors:
  - https://blossom.example.net
```

- **The folder.** With `blossom_retire_days`, the public files in `blossom-retired/` of the community folder (beside
  `theme/`, with the same extensions and rules) are served by their hash too. They are not in `/list`.
- **For some days, with `Sunset`.** A retired file is served for that many days from when bitmentor first sees it in
  the folder, with the header `Sunset` saying until when (BUD-01) and a cache of a day at most. After that it is not
  served, even if it is still in the folder, so the folder can be emptied whenever it suits.
- **Then where it is, or gone.** Past its days, if a server of `blossom_mirrors` has the same hash the answer is a
  `308` to it (BUD-01 allows a redirect to an address with the same hash); otherwise a `410` with `X-Reason: retired`,
  naming the hash now at the same path of the theme when there is one; both are cached for an hour. bitmentor remembers the retired hashes in its
  database, so it still answers when the file is no longer in the folder. A hash the theme serves again is no longer retired:
  put aside again later, it has its days again.
- **The mirrors.** `blossom_mirrors` lists up to 10 other Blossom servers, `https://` and a host only. bitmentor asks
  them `HEAD /<sha256>` in the background, at most once a day for each retired file, never while a visitor waits, and
  follows no redirect. A mirror has the file when it answers 200 with the file's own length; one that does not answer
  changes nothing, and is asked again in an hour. Addresses in numbers and local names are not taken. It does not
  upload to them: someone has to have copied the files there (BUD-04).

**Putting the old files aside.** [`deploy/blossom-retire.py`](../../deploy/blossom-retire.py) does it, on the
machine, after the community folder was updated; it needs only Python 3. It copies to `blossom-retired/` every public
file the theme had before whose bytes are in no file of the theme now, under the same path (a second old version of
the same path gets the start of its hash in the name).

```
deploy/blossom-retire.py /srv/community --git ORIG_HEAD            # a git checkout, after a pull
deploy/blossom-retire.py /srv/community --from /srv/community.old  # an older copy of the folder
deploy/blossom-retire.py /srv/community --prune 60                 # removes what was put aside over 60 days ago
```

`--prune` can go with the other two, so one line after each update keeps the folder small. It goes by the date of
the files, which the tool sets when it puts them aside: give it at least the days of `blossom_retire_days`, or a file
goes before the `Sunset` it announced, and `touch` a file you put in the folder by hand. A community that keeps its
old files another way only has to put them in the folder.

**Which files.** The files of `theme/` (the ones served at `/theme/…`) with an extension of a picture, a document, sound,
video or an archive (`png jpg jpeg webp gif avif svg ico pdf epub mp3 ogg opus m4a wav flac mp4 webm mov zip gz tar tgz
woff2`). Not served: hidden files and folders, links, styles, scripts, JSON data, anything outside `theme/`. If two files
have the same bytes, the hash points to the first by path.

**The index.** Hash → file is built the first time it is asked, in memory, and looked at again at most every 30 seconds.
A file is read for its hash only the first time and when its size or modification time changes; large media are not read
on every request. The index is lost when the process restarts and built again on the first request.

**What it does not do.** It does not take uploads, does not check who asks (BUD-02, BUD-04 and the other BUDs are not
there), does not keep copies of other people's files, does not upload to other servers, and does not promise anything about other servers: a file lives on
as long as someone keeps it.
