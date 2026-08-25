"""Fuzzy matching between Deezer releases and Gazelle torrent groups.

Deezer and the trackers disagree constantly: composer prefixes, edition suffixes,
alias titles, "Various Artists" on soundtracks, and classical releases credited
to a conductor on one side and an orchestra on the other. These heuristics were
tuned against real RED/OPS data and are kept as-is rather than replaced with a
generic string distance.
"""

import re
from typing import Any

EDITION_KEYWORDS = frozenset(
    {
        "remaster",
        "remastered",
        "deluxe",
        "edition",
        "expanded",
        "extended",
        "version",
        "box",
        "set",
        "complete",
        "ultimate",
        "explicit",
        "clean",
        "live",
        "acoustic",
        "unplugged",
        "anniversary",
        "mono",
        "stereo",
        "cinematic",
        "chocolate",
        "original",
        "official",
        "soundtrack",
        "motion",
        "picture",
        "netflix",
        "film",
        "initial",
        "release",
        "essentials",
        "excerpts",
    }
)

CLASSICAL_MARKERS = (
    "bach",
    "beethoven",
    "mozart",
    "mahler",
    "prokofiev",
    "ravel",
    "symphony",
    "suite",
    "op.",
    "bwv",
    "philharmonic",
    "orchestra",
    "choeur",
    "choir",
    "concerto",
    "sonata",
)

SOUNDTRACK_MARKERS = (
    "soundtrack",
    "motion picture",
    "from the netflix film",
    "original score",
    "official soundtrack",
    "game soundtrack",
)

_EDITION_SUFFIX_RE = re.compile(
    r"\s*(special\s*edition|deluxe|remix|expanded|remastered|edition|version|ep|lp|single|\d{4}\s*remixes?).*$",
    re.IGNORECASE,
)

# Every way the two sides write "these people made this record together".
# This replaced a pair of one-sided featuring regexes that needed whitespace on
# both sides of their separator, and so never split "Jigitz, Tabi".
_CREDIT_SPLIT_RE = re.compile(
    r"\s*(?:"
    r"&|\+|;|,"                                     # punctuation joins
    r"|\s/\s"                                       # a spaced slash; AC/DC survives
    r"|\b(?:featuring|feat|ft|with|vs|and|x)\b\.?"  # word joins, whole words only
    r")\s*",
    re.IGNORECASE,
)
"""Whole words only. Without the boundaries this splits inside "Alexander" and
"Texas", turning one artist into two and matching them against anything."""

VARIOUS_ARTISTS = frozenset({"variousartists", "various", "va", "variousartist", "compilation"})
"""What a compilation is credited to when nobody is credited."""


def credits_of(value: str | None) -> list[str]:
    """Every artist named in one credit string, normalized, in order.

    "Jigitz & Tabi" is two credits, not one artist whose name happens to
    contain an ampersand. Comparing the whole string treats it as the latter,
    which is why a release the tracker credits to a duo did not match the same
    release Deezer credits to its lead.

    Args:
        value: A credit string from either side.

    Returns:
        Normalized names, first one first, without duplicates.
    """
    if not value:
        return []
    names: list[str] = []
    for part in _CREDIT_SPLIT_RE.split(value):
        name = normalize(part)
        if name and name not in names:
            names.append(name)
    return names


def normalize(value: str | None) -> str:
    """Reduce a string to lowercase alphanumerics, with ``&`` spelled out."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("&", "and"))


def normalize_keep_spaces(value: str | None) -> str:
    """Like :func:`normalize` but keeps word boundaries."""
    if not value:
        return ""
    collapsed = re.sub(r"[^\w\s]", " ", value.lower().replace("&", "and"))
    return re.sub(r"\s+", " ", collapsed).strip()


def similarity(a: str | None, b: str | None) -> float:
    """Jaccard similarity over the character sets of two normalized strings."""
    if not a or not b:
        return 0.0
    a_norm, b_norm = normalize(a), normalize(b)
    if a_norm == b_norm:
        return 1.0
    set_a, set_b = set(a_norm), set(b_norm)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def strip_parenthetical(value: str | None) -> str:
    """Remove bracketed and parenthesised segments."""
    if not value:
        return ""
    stripped = re.sub(r"\([^)]*\)", "", value)
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def strip_prefix(title: str) -> str:
    """Drop a leading ``Composer: `` or ``Composer - `` style prefix.

    Only prefixes of one to three non-numeric words are removed, so titles like
    ``2001: A Space Odyssey`` survive intact.
    """
    if ":" in title:
        head, tail = title.split(":", 1)
        prefix = head.strip()
        if 1 <= len(prefix.split()) <= 3 and not prefix.isdigit():
            return tail.strip()
    match = re.match(r"^([A-Za-z][A-Za-z\s.\-]+?)\s*-\s+(.+)$", title)
    if match:
        prefix = match[1].strip()
        if 1 <= len(prefix.split()) <= 3 and not prefix.isdigit():
            return match[2].strip()
    return title


def base_title(title: str) -> str:
    """Reduce a title to its core words, dropping editions and years."""
    reduced = strip_prefix(strip_parenthetical(title))
    words = [w for w in normalize_keep_spaces(reduced).split() if w not in EDITION_KEYWORDS]
    return " ".join(w for w in words if not (w.isdigit() and len(w) == 4)).strip()


def parse_alias_title(title: str) -> tuple[str | None, str | None]:
    """Split an ``Alias: Edition`` title into its two halves.

    Returns:
        Tuple of (alias, edition), or (None, None) when the title is not of
        that shape.
    """
    if ":" not in title:
        return None, None
    alias, edition = (part.strip() for part in title.split(":", 1))
    if not (1 <= len(alias.split()) <= 3 and not alias.isdigit()):
        return None, None
    return alias, edition


def title_matches(deezer_title: str, tracker_title: str) -> tuple[bool, float]:
    """Compare two release titles across several normalizations.

    Returns:
        Tuple of (matched, best score). A score of 0.85 or better counts as a
        match even without an exact normalized hit.
    """
    if not deezer_title or not tracker_title:
        return False, 0.0

    pairs = (
        (deezer_title, tracker_title),
        (strip_prefix(deezer_title), tracker_title),
        (deezer_title, strip_prefix(tracker_title)),
        (strip_prefix(deezer_title), strip_prefix(tracker_title)),
        (base_title(deezer_title), base_title(tracker_title)),
    )
    for left, right in pairs:
        if left and right and normalize(left) == normalize(right):
            return True, 1.0

    best = max(similarity(left, right) for left, right in pairs)
    return best >= 0.85, best


def is_classical(deezer_title: str, tracker_title: str) -> bool:
    """True when either title looks like a classical release."""
    text = normalize_keep_spaces(f"{deezer_title} {tracker_title}")
    return any(marker in text for marker in CLASSICAL_MARKERS)


def is_soundtrack(title: str, record_type: str | None) -> bool:
    """True when a release looks like a soundtrack."""
    if record_type and record_type.lower() == "soundtrack":
        return True
    lowered = (title or "").lower()
    return any(marker in lowered for marker in SOUNDTRACK_MARKERS)


def artist_matches(
    candidate: str,
    tracker_artist: str,
    deezer_title: str,
    tracker_title: str,
    soundtrack: bool = False,
) -> bool:
    """Decide whether a Deezer artist credit matches a tracker artist credit.

    Args:
        candidate: One artist name from the Deezer side.
        tracker_artist: The tracker group's artist string.
        deezer_title: Deezer release title, used for the classical fallback.
        tracker_title: Tracker release title, used for the classical fallback.
        soundtrack: Whether the release is a soundtrack, which lets a
            "Various Artists" tracker credit match anything.

    Returns:
        True when the credits are compatible.
    """
    if not candidate:
        return False
    if not tracker_artist:
        return True

    candidate_norm, tracker_norm = normalize(candidate), normalize(tracker_artist)
    if soundtrack and tracker_norm == "variousartists":
        return True
    if candidate_norm == tracker_norm or candidate_norm in tracker_norm or tracker_norm in candidate_norm:
        return True

    candidate_words = set(normalize_keep_spaces(candidate).split())
    tracker_words = set(normalize_keep_spaces(tracker_artist).split())
    if candidate_words and tracker_words and candidate_words <= tracker_words:
        return True

    if is_classical(deezer_title, tracker_title):
        if candidate_words & tracker_words:
            return True
        if any(len(word) > 3 and word in normalize(tracker_title) for word in candidate_words):
            return True

    overlap = candidate_words & tracker_words
    if len(overlap) >= 2:
        return True
    return len(candidate_words) == 1 and bool(overlap)


def has_web_flac(torrents: list[dict]) -> bool:
    """True when a torrent group contains a lossless WEB FLAC."""
    for torrent in torrents:
        if (
            (torrent.get("media") or "").upper() == "WEB"
            and (torrent.get("format") or "").upper() == "FLAC"
            and "LOSSLESS" in (torrent.get("encoding") or "").upper()
        ):
            return True
    return False


def edition_in_remaster(edition: str | None, remaster_titles: list[str]) -> bool:
    """True when an edition string is reflected in any torrent's remaster title."""
    if not edition:
        return False
    edition_norm = normalize(edition)
    for title in remaster_titles:
        if not title:
            continue
        title_norm = normalize(title)
        if edition_norm == title_norm or edition_norm in title_norm or title_norm in edition_norm:
            return True
        if similarity(edition, title) >= 0.85:
            return True
    return False


def artists_of(deezer_info: dict) -> list[str]:
    """Collect every artist credit from a public-API album payload."""
    artists: list[str] = []
    primary = (deezer_info.get("artist") or {}).get("name")
    if primary:
        artists.append(primary)
    for contributor in deezer_info.get("contributors") or []:
        name = contributor.get("name")
        if name and name not in artists:
            artists.append(name)
    return artists


def evaluate_group(deezer_info: dict, group: dict) -> tuple[bool, str]:
    """Decide whether a tracker torrent group is the Deezer release.

    Args:
        deezer_info: Public-API album payload.
        group: A ``torrentgroup`` response containing ``group`` and ``torrents``.

    Returns:
        Tuple of (matched, human-readable reason).
    """
    title = deezer_info.get("title", "")
    soundtrack = is_soundtrack(title, (deezer_info.get("record_type") or "").lower())

    group_info = group.get("group") or {}
    torrents = group.get("torrents") or []
    tracker_title = group_info.get("name", "")
    tracker_artist = group_info.get("artist", "")

    matched, score = title_matches(title, tracker_title)

    alias, edition = parse_alias_title(title)
    if alias and not matched:
        alias_matched, alias_score = title_matches(alias, tracker_title)
        if alias_matched:
            remaster_titles = [t.get("remasterTitle", "") for t in torrents]
            edition_words = set(normalize_keep_spaces(edition or "").split())
            if edition_in_remaster(edition, remaster_titles) or (edition_words & EDITION_KEYWORDS):
                matched, score = True, alias_score

    if not matched:
        return False, f"title mismatch ({score:.2f})"

    artists = artists_of(deezer_info)
    if alias and alias not in artists:
        artists.append(alias)

    if tracker_artist and not any(
        artist_matches(artist, tracker_artist, title, tracker_title, soundtrack) for artist in artists
    ):
        return False, f"artist mismatch: Deezer={artists} vs tracker={tracker_artist}"

    if not has_web_flac(torrents):
        return False, "no WEB FLAC in group"

    return True, "match"


def build_search_queries(deezer_info: dict) -> list[str]:
    """Build the tracker search strings to try for a Deezer release.

    Ordered cheapest-to-broadest so a match is usually found on the first query
    and the rest never cost a request.

    Args:
        deezer_info: Public-API album payload.

    Returns:
        De-duplicated search strings.
    """
    title = deezer_info.get("title", "")
    artist = (deezer_info.get("artist") or {}).get("name", "")
    queries: list[str] = []

    base = base_title(title)
    if artist and base:
        queries.append(f"{artist} {base}")

    stripped = strip_prefix(title)
    if stripped != title:
        if artist:
            queries.append(f"{artist} {stripped}")
        queries.append(stripped)

    alias, edition = parse_alias_title(title)
    if alias:
        queries.append(alias)
        if edition:
            queries.append(f"{alias} {edition}")
        if artist:
            queries.append(f"{artist} {alias}")

    if base:
        queries.append(base)
    if title:
        queries.append(title)

    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        cleaned = query.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


# ----------------------------------------------------------------------
# Request filling: tracker request -> best Deezer album
# ----------------------------------------------------------------------

MIN_ARTIST_SCORE = 0.50
MIN_TITLE_SCORE = 0.40
MIN_TOTAL_SCORE = 0.70


def _score_artist(artist: str, dz_artist: str) -> float:
    """Score how well two artist credits describe the same act.

    Symmetric, because the two sides disagree in both directions: a tracker
    credits a duo where Deezer credits its lead, and just as often the reverse.
    The old scoring only ever looked for a collaboration on the left, so
    ("Jigitz & Tabi", "Jigitz") scored 0.90 and ("Jigitz", "Jigitz & Tabi")
    scored 0.15 -- the same two names, rejected in one direction and accepted in
    the other. That is what filed a release already on OPS under "not on
    tracker".

    Args:
        artist: One side's credit string.
        dz_artist: The other side's credit string.

    Returns:
        0.0 to 1.0. MIN_ARTIST_SCORE is the floor for a usable match.
    """
    artist_norm, dz_norm = normalize(artist), normalize(dz_artist)
    if not artist_norm or not dz_norm:
        return 0.0
    if artist_norm == dz_norm:
        return 1.0
    if re.sub(r"^the", "", artist_norm) == re.sub(r"^the", "", dz_norm):
        return 0.98

    left, right = credits_of(artist), credits_of(dz_artist)
    left_set, right_set = set(left), set(right)

    # A compilation credited to nobody in particular. It cannot corroborate the
    # artist, so it scores just over the floor and leaves the decision to the
    # title: at 0.55 the total only clears MIN_TOTAL_SCORE when the title is
    # very nearly exact, which is the right bar for "Various Artists — Eden
    # Sauvage" against "Los Eclipses — Eden Sauvage".
    if (left_set & VARIOUS_ARTISTS) or (right_set & VARIOUS_ARTISTS):
        return 0.55

    if left_set and right_set and left_set != right_set:
        if left_set == right_set:
            return 0.97
        # One side names a subset of the other: the same act, credited to more
        # or fewer of the people on it.
        if left_set <= right_set or right_set <= left_set:
            return 0.88
        shared = left_set & right_set
        if shared:
            # Sharing the lead credit is a much stronger signal than sharing a
            # guest, so the two cases do not score the same.
            leads_shared = bool(left) and bool(right) and (left[0] in right_set or right[0] in left_set)
            return 0.78 if leads_shared else 0.60
    elif left_set and left_set == right_set:
        return 0.97

    if artist_norm and dz_norm and (artist_norm in dz_norm or dz_norm in artist_norm):
        ratio = min(len(artist_norm), len(dz_norm)) / max(len(artist_norm), len(dz_norm))
        if ratio >= 0.85:
            return 0.70
        if ratio >= 0.70:
            return 0.50
        return 0.30 if ratio >= 0.50 else 0.15

    return min(similarity(artist, dz_artist) * 0.5, 0.35)


def _score_title(album: str, dz_title: str) -> float:
    """Score how well a request's album title matches a Deezer album's title."""
    album_norm, dz_norm = normalize(album), normalize(dz_title)
    if album_norm == dz_norm:
        return 1.0

    if album_norm and dz_norm and (album_norm in dz_norm or dz_norm in album_norm):
        ratio = min(len(album_norm), len(dz_norm)) / max(len(album_norm), len(dz_norm))
        if ratio >= 0.80:
            return 0.70
        return 0.45 if ratio >= 0.60 else 0.20

    album_base = normalize(_EDITION_SUFFIX_RE.sub("", album).strip())
    title_base = normalize(_EDITION_SUFFIX_RE.sub("", dz_title).strip())
    if not (album_base and title_base):
        return min(similarity(album, dz_title) * 0.4, 0.25)
    if album_base == title_base:
        return 0.88
    if album_base in title_base or title_base in album_base:
        ratio = min(len(album_base), len(title_base)) / max(len(album_base), len(title_base))
        return 0.65 if ratio >= 0.70 else 0.35
    return min(similarity(album, dz_title) * 0.45, 0.30)


def find_best_deezer_match(
    albums: list[dict],
    artist: str,
    album: str,
    expected_tracks: int | None = None,
) -> tuple[dict | None, float, dict[str, Any]]:
    """Pick the Deezer album that best fills a tracker request.

    Every threshold must be cleared independently — a great artist score cannot
    rescue a poor title score — and when the expected track count is known it
    must match exactly. Filling a request with the wrong release is worse than
    not filling it.

    Args:
        albums: Deezer album search results.
        artist: Artist name from the request.
        album: Album title from the request.
        expected_tracks: Track count derived from the request description or its
            external links, when one could be determined.

    Returns:
        Tuple of (best album or None, score, details about the winner).
    """
    best_album: dict | None = None
    best_score = 0.0
    best_details: dict[str, Any] = {}

    for candidate in albums:
        dz_artist = (candidate.get("artist") or {}).get("name", "")
        dz_title = candidate.get("title", "")
        dz_tracks = candidate.get("nb_tracks")

        if expected_tracks is not None and dz_tracks is not None and dz_tracks != expected_tracks:
            continue

        artist_score = _score_artist(artist, dz_artist)
        if artist_score < MIN_ARTIST_SCORE:
            continue

        title_score = _score_title(album, dz_title)
        if title_score < MIN_TITLE_SCORE:
            continue

        total = (artist_score * 0.60) + (title_score * 0.40)
        if artist_score >= 0.95 and title_score >= 0.85:
            total = min(1.0, total * 1.05)
        if total < MIN_TOTAL_SCORE or total <= best_score:
            continue

        best_score = total
        best_album = candidate
        best_details = {
            "artist_score": round(artist_score, 3),
            "title_score": round(title_score, 3),
            "dz_artist": dz_artist,
            "dz_title": dz_title,
            "dz_year": (candidate.get("release_date") or "")[:4],
            "dz_tracks": dz_tracks,
        }

    return best_album, best_score, best_details
