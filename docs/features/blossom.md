# Blossom: the site's files by their hash

With `blossom: true` in `community.yml` the site also answers as a read-only [Blossom](https://github.com/hzrd149/blossom)
server (BUD-01) for the community's public files. Anyone can ask for a file by its SHA-256, check that the bytes match
the hash, and copy it to another Blossom server, so the files outlive this one. There is no upload, no authorization and
no new service: it is a view over the files the site already serves. Off by default; off, every address below is a 404.

| Address | What it does |
|---|---|
| `GET`/`HEAD /blossom/<sha256>` and `/blossom/<sha256>.<ext>` | The file, with its `Content-Type` and `Content-Length`, `Access-Control-Allow-Origin: *`, `Cache-Control: public, max-age=31536000, immutable` (the content of a hash never changes) and `Range`. `OPTIONS` answers the CORS preflight. A hash the site does not have is a 404. The extension is a hint only: the hash decides |
| `GET /blossom/list` | JSON `{"server": …, "files": [{"sha256", "size", "type", "url", "blossom"}]}`: for each file, its hash, size, type, the normal address (`/theme/…`) and the Blossom one |

**The prefix.** BUD-01 puts the hash at the root (`/<sha256>`), but the root of the site belongs to the language
prefixes and pages, so the files sit under `/blossom/`. A Blossom client builds `<server>/<sha256>`, so the server to give
it is `https://<site>/blossom`.

**Which files.** The files of `theme/` (the ones served at `/theme/…`) with an extension of a picture, a document, sound,
video or an archive (`png jpg jpeg webp gif avif svg ico pdf epub mp3 ogg opus m4a wav flac mp4 webm mov zip gz tar tgz
woff2`). Not served: hidden files and folders, links, styles, scripts, JSON data, anything outside `theme/`. If two files
have the same bytes, the hash points to the first by path.

**The index.** Hash → file is built the first time it is asked, in memory, and looked at again at most every 30 seconds.
A file is read for its hash only the first time and when its size or modification time changes; large media are not read
on every request. The index is lost when the process restarts and built again on the first request.

**What it does not do.** It does not take uploads, does not check who asks (BUD-02, BUD-04 and the other BUDs are not
there), does not keep copies of other people's files, and does not promise anything about other servers: a file lives on
as long as someone keeps it.
