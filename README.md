# lox

**A Deezer-to-Gazelle upload pipeline with a web UI.** Find a release on Deezer, check whether RED and OPS already have
it, look at what the checker found, download it in FLAC, and upload it to whichever trackers are missing it — without
leaving the browser.

> **This is a vibe-coded fork of [smoked-salmon](https://github.com/smokin-salmon/smoked-salmon) `0.10.0`.** It is not
> maintained, not reviewed, and not affiliated with upstream. If you want the real, supported thing, use
> [smokin-salmon/smoked-salmon](https://github.com/smokin-salmon/smoked-salmon). Upstream owns the tagging, spectral,
> transcoding and upload machinery this is built on; everything Deezer-shaped here is the fork.

---

## The one thing to understand first

**Trackers are only contacted when you press a button that says so.**

RED and OPS have small API budgets and punish bursts with hours-long lockouts. So every tracker call in this app goes
through a single gateway that spends from a token bucket, spaces calls apart, and opens a circuit breaker after repeated
failures. Search, Explore, album metadata, FLAC checks and downloads never touch a tracker at all.

The sidebar shows the remaining budget per tracker at all times. When a scan would overdraw it, the scan **stops early
and keeps its place** rather than blowing the limit.

---

## What it does

### Check one album, then upload it

Open any album and press **Check RED** or **Check all**. The checker searches the tracker, opens each candidate torrent
group, and tells you what it found — including the groups it *rejected* and why:

```
RED · not on tracker · 4 call(s), 2 search(es) · artist page
  ├─ Ana Vidal — Nocturne Variations (Remixes) (2026)   title mismatch (0.81)     WEB FLAC Lossless
  └─ Anna Vidale — Nocturnes (2019)                     artist mismatch            CD FLAC Lossless
OPS · not on tracker · 2 call(s), 1 search(es)
  └─ Ana Vidal — Nocturne Variations (2026)             no WEB FLAC in group       CD MP3 320
```

Every group is a link. Open them, confirm the checker got it right, then press **Download & upload to RED + OPS**. The
near misses are the point — a "missing" verdict is only trustworthy if you can see what it looked at.

### Check a list of requests against Deezer

Paste request IDs or URLs, or upload a `.txt` list. For each request lox fetches it from the tracker, searches Deezer,
scores the match, then rejects it unless it clears every gate independently:

| Gate | Rule |
|---|---|
| Artist score | ≥ 0.50 |
| Title score | ≥ 0.40 |
| Combined | ≥ `checker.min_confidence` (0.70) |
| Track count | Exact match when it can be determined |
| Availability | Every track streamable in your region |
| FLAC | All tracks lossless, when the request is FLAC-only |
| External sources | Discogs, MusicBrainz, Bandcamp, Beatport, Qobuz, Tidal, Apple Music and Metal-Archives must agree on the track count |

Filling a request with the wrong edition is worse than not filling it, so a great artist score cannot rescue a poor title
score. Only the request fetch costs tracker budget; everything after it is free.

### Saved Deezer searches

The **Missing** tab keeps re-runnable queries — new releases by genre, a chart, an album search, an artist's
discography, a playlist, or a channel module. Running one is free and drops its albums straight into the collect step.
Set one up for new releases in your genre and it becomes a two-click routine.

### Explore

Deezer channels, charts by genre, and editorial new releases. Channels are read out of the page's `__DZR_APP_STATE__`,
which is the surface deemix never exposed. Any channel module can be sent to the Missing tab with one click.

### Downloading

With an ARL set, downloads happen in-process: stream URLs resolved through Deezer's media API, the Blowfish-striped
payload decrypted as it streams, tags and cover art written from the Deezer metadata. The result is a plain release
folder the upload flow already understands.

### Hardlinked per-tracker folders

Uploading one release to two trackers needs two torrents pointed at two paths. Rather than keep two copies, lox
hardlinks — the same thing cross-seed does:

```
/links/
├── RED/
│   └── Ana Vidal - Nocturne Variations (2026) [WEB FLAC]/
│       ├── 01. Nocturne I.flac    → hardlink
│       └── cover.jpg              → hardlink
└── OPS/
    └── Ana Vidal - Nocturne Variations (2026) [WEB FLAC]/
        ├── 01. Nocturne I.flac    → hardlink
        └── cover.jpg              → hardlink
```

A 500 MB release costs 500 MB no matter how many trackers it goes to. Point qBittorrent at `/links` and both torrents
seed from their own directory.

**Hardlinks cannot cross filesystems.** `linking.link_dir` must be on the same volume as your downloads. If it isn't,
lox fails loudly rather than silently making real copies — unless you set `linking.fallback_to_copy = true`.

---

## Install

Needs Python 3.11+, plus `sox`, `flac`, `lame` and `mp3val` on PATH.

```bash
uv tool install git+https://github.com/MonkeyGoneWIld/lox
```

Then write a config (see [`data/config.default.toml`](data/config.default.toml) for every option) and run:

```bash
lox ui
```

The UI comes up on `http://127.0.0.1:55015` — change it under `[upload.web_interface]`. The `salmon` command still works
as an alias, and every upstream CLI command is unchanged.

---

## Docker

```bash
cp .env.example .env
openssl rand -hex 32   # put this in LOX_AUTH_TOKEN
cp config/config.docker.toml config/config.toml   # then fill in your credentials
docker compose up -d
```

[`docker-compose.yml`](docker-compose.yml) is annotated. Two things will bite you if you skip them:

**Set `LOX_AUTH_TOKEN`.** The UI can spend your tracker API budget, read your authenticated Deezer session, and start
uploads to your tracker accounts. The compose file binds to `127.0.0.1` by default for exactly this reason. If you
publish the port, the token is the only thing standing between the internet and your upload privileges. lox prints a
loud warning at startup if you bind publicly without one.

**Put `/downloads` and `/links` on the same filesystem.** Different volumes means no hardlinks.

Open the UI once with `?token=<your token>` to set the session cookie.

---

## Configuration

Everything lives in `config.toml`, which is gitignored. No credential is ever written to the repo.

| Section | Key settings |
|---|---|
| `[metadata.deezer]` | `arl`, `download_dir`, `preferred_format`, `format_fallback`, `concurrent_downloads` |
| `[linking]` | `enabled`, `link_dir`, `method`, `per_tracker_dirs`, `fallback_to_copy` |
| `[checker]` | `tracker_budget`, `tracker_budget_window`, `tracker_call_delay`, `failure_threshold`, `cooldown_seconds`, `min_tracks`, `min_date`, `min_confidence` |
| `[upload.web_interface]` | `host`, `port`, `auth_token` |
| `[notifications]` | `enabled`, `discord_webhook`, `notify_missing`, `notify_fillable` |
| `[tracker.red]` / `[tracker.ops]` | `session`, `api_key` |
| `[metadata]` | `discogs_token`, `apple_music_token`, Qobuz and Tidal credentials — all used to verify request track counts |

### Getting your ARL

Log into Deezer, open developer tools → Application → Cookies → `https://www.deezer.com`, and copy the `arl` value. It
is a full session credential: anyone holding it is logged into your account. Treat it like a password.

---

## Differences from upstream smoked-salmon

- **Deezer only** — metadata search is restricted to Deezer. Bandcamp, Beatport, Discogs, iTunes, JunoDownload,
  MusicBrainz, Qobuz and Tidal are disabled as *sources*, though several are still used to verify request track counts.
- **Folders are moved, not copied** — releases are moved into `download_directory` with `shutil.move`, and an emptied
  source parent is removed. The `hardlinks` and `remove_source_dir` options are gone; use `[linking]` instead.
- **Lossy-master prompts always ask** — even with `yes_all`. Auto-answering "no" asserts something about a release
  nobody looked at. In the web UI the prompt appears in the upload console and you answer it there.
- **No upstream footer** — the "Uploaded with smoked-salmon" line is dropped from torrent and transcode descriptions.
- **Icons** point at `img.onlyimage.org` rather than `ptpimg.me`.

---

## How it fits together

```
salmon/deezer/     gw.py, crypto.py, download.py, explore.py
                   Private gw-light API, Blowfish stream decryption, channels

salmon/checker/    gateway.py    every tracker call, budgeted and breakered
                   matching.py   title/artist/edition heuristics, request scoring
                   missing.py    collect (free) then check (budgeted)
                   requests_check.py + trackcount.py
                   watchlists.py saved searches
                   store.py      debounced atomic JSON state

salmon/seeding/    links.py      hardlinked per-tracker folders

salmon/web/        api.py        JSON API, auth middleware, path validation
                   jobs.py       background jobs with progress
                   static/       no-build SPA
```

---

## Status and caveats

Written fast, verified where it could be. The UI was driven end to end against a mock API — every tab, every flow — and
the Python is syntax- and import-clean. **What has not been exercised against the real thing:** the download chain
(Deezer's media token flow and the Blowfish decryption), the channel page scraping, and live tracker calls under real
rate limits. Deezer changes those surfaces; expect the download path to be the first thing that breaks.

The tracker budget defaults are conservative guesses, not measured limits. Tune `[checker]` to what your trackers
actually allow.

---

## Licence

Apache-2.0, inherited from upstream. deemix is GPL-3.0 and none of its code is used here — the UI resembles it, it does
not reuse it.
