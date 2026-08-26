# IBM Plex, vendored

Served from this host rather than linked from `fonts.googleapis.com`. A link
there announces every page load of a private instance — one holding tracker
sessions and a Deezer ARL — to a third party, and leaves the interface
looking wrong on a LAN with no route out.

Latin and latin-ext only. The other thirty subsets Google serves are scripts
this interface never renders, and they would quadruple the size for nothing.

IBM Plex is licensed under the SIL Open Font License 1.1.

## Regenerating

    curl -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120" \
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" \
      -o /tmp/gf.css

The user agent matters: without a modern one Google serves ttf instead of
woff2. Then pull the `latin` and `latin-ext` blocks out of that CSS, download
each `url(...)`, and write `../css/fonts.css` with the same `@font-face`
rules pointed at the local copies. Keep `unicode-range` exactly as Google
gives it — it is what stops the browser fetching a face for text that has no
character in it.
