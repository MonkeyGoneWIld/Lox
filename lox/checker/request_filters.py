"""What each tracker's request search actually accepts.

Both sites run Gazelle, and the request search looks identical on both. It is
not. The parameter names differ, and — the part that quietly ruins a search —
so do the numeric IDs behind the labels:

    Media "WEB"           RED 7    OPS 1
    Encoding "Lossless"   RED 8    OPS 0
    Encoding "320"        RED 7    OPS 5
    Release "Demo"        RED 17   OPS 10
    Tag mode              RED tags_type=0|1    OPS tag_mode=any|all

So a single shared table would not merely fail on one of them, it would succeed
with the wrong filter: ask OPS for WEB using RED's ID and you get DAT, with no
error anywhere. Everything here is transcribed from the two live search forms
rather than from the Gazelle source, so it matches what those two sites are
running now.

Semantic names go in — "FLAC", "WEB", "Lossless" — and per-tracker parameters
come out. A tracker with no entry here gets the filters that need no IDs, which
is the only safe default: sending an unmapped number would filter for whatever
that number happens to mean there.
"""

from typing import Any, NamedTuple

PAGE_SIZE = 25
"""Requests per page. Both sites paginate 1-25, 26-50, and neither takes a page
size parameter, so this is what one tracker call buys."""


#: Stands for "every option in this group", so a default set does not have to
#: repeat seventeen release types and go stale the day a tracker adds one.
ALL: tuple[str, ...] = ("*",)


#: The seven categories, in the order both forms list them. Same labels, same
#: order, different numbering: RED starts at 1 and OPS at 0.
CATEGORIES: tuple[str, ...] = (
    "Music", "Applications", "E-Books", "Audiobooks", "E-Learning Videos", "Comedy", "Comics",
)


class TrackerFilters(NamedTuple):
    """One tracker's request-search vocabulary."""

    tag_mode_param: str
    tag_mode_any: str
    tag_mode_all: str
    formats: dict[str, int]
    media: dict[str, int]
    encodings: dict[str, int]
    release_types: dict[str, int]
    formats_strict_param: str
    media_strict_param: str
    encodings_strict_param: str
    #: Category label to the id this tracker files it under. Both sites list the
    #: same seven in the same order and number them differently: RED starts at
    #: 1, OPS at 0.
    categories: dict[str, int]
    #: How the tracker spells a category selection. RED indexes them --
    #: filter_cat[1]=1 -- and OPS collects them -- filter_cat[]=0. Sending one
    #: site's spelling to the other filters nothing and returns everything.
    category_style: str
    #: What this site calls the encoding group. RED says "Bitrates", OPS says
    #: "Encoding", and the page should say what the site the user is searching
    #: says.
    encodings_label: str
    #: The order the site puts its groups in. RED goes formats, bitrates,
    #: media; OPS goes media, formats, encoding. Same three groups, different
    #: order, and a form that claims to mirror the site has to mirror that too.
    group_order: tuple[str, ...]
    #: Whether the category row offers an All. RED's does not -- it is seven
    #: bare boxes, and leaving them all clear is how you ask for everything.
    categories_all: bool = True
    #: Whether the categories start ticked. OPS ticks them; RED leaves them
    #: clear, which on RED means the same thing.
    categories_default: bool = True
    #: Which boxes are already ticked when the page opens, per group. This is
    #: the search almost everyone actually runs -- music, on the web, in a
    #: format Deezer can produce -- so it is what the form arrives set to
    #: rather than something to be reconstructed by hand every visit. ``ALL``
    #: means every option in that group; a group left out of the mapping also
    #: starts fully ticked.
    defaults: dict[str, tuple[str, ...]] = {}
    #: Toggles that start on. RED's "include old" does, because a request
    #: nobody has touched in a year is still a request.
    toggle_defaults: tuple[str, ...] = ()
    supports_bounty: bool = False
    supports_include_old: bool = False
    supports_descriptions: bool = False


RED = TrackerFilters(
    tag_mode_param="tags_type",
    tag_mode_any="0",
    tag_mode_all="1",
    formats={"MP3": 0, "FLAC": 1, "AAC": 2, "AC3": 3, "DTS": 4, "DSD": 5},
    media={"CD": 0, "DVD": 1, "Vinyl": 2, "Soundboard": 3, "SACD": 4, "DAT": 5, "Cassette": 6, "WEB": 7,
           "Blu-Ray": 8},
    encodings={"192": 0, "APS (VBR)": 1, "V2 (VBR)": 2, "V1 (VBR)": 3, "256": 4, "APX (VBR)": 5, "V0 (VBR)": 6,
               "320": 7, "Lossless": 8, "24bit Lossless": 9, "DSD64": 10, "DSD128": 11, "DSD256": 12,
               "DSD512": 13, "Other": 14},
    release_types={"Album": 1, "Soundtrack": 3, "EP": 5, "Anthology": 6, "Compilation": 7, "Single": 9,
                   "Live album": 11, "Remix": 13, "Bootleg": 14, "Interview": 15, "Mixtape": 16, "Demo": 17,
                   "Concert Recording": 18, "DJ Mix": 19, "Unknown": 21},
    formats_strict_param="formats_strict",
    media_strict_param="media_strict",
    # Singular on RED, plural on OPS. Nothing warns you when it is wrong.
    encodings_strict_param="bitrate_strict",
    categories=dict(zip(CATEGORIES, range(1, len(CATEGORIES) + 1), strict=True)),
    category_style="indexed",
    encodings_label="Bitrates",
    group_order=("release_types", "formats", "encodings", "media"),
    categories_all=False,
    categories_default=False,
    defaults={
        "categories": ("Music",),
        "release_types": ALL,
        "formats": ("MP3", "FLAC"),
        "encodings": ("V0 (VBR)", "320", "Lossless"),
        "media": ("WEB",),
    },
    toggle_defaults=("include_old",),
    supports_include_old=True,
    supports_descriptions=True,
)

OPS = TrackerFilters(
    tag_mode_param="tag_mode",
    tag_mode_any="any",
    tag_mode_all="all",
    formats={"MP3": 0, "FLAC": 1, "Ogg Vorbis": 2, "AAC": 3, "AC3": 4, "DTS": 5},
    media={"CD": 0, "WEB": 1, "Vinyl": 2, "DVD": 3, "BD": 4, "Soundboard": 5, "SACD": 6, "DAT": 7,
           "Cassette": 8},
    encodings={"Lossless": 0, "24bit Lossless": 1, "V0 (VBR)": 2, "V1 (VBR)": 3, "V2 (VBR)": 4, "320": 5,
               "256": 6, "192": 7, "160": 8, "128": 9, "96": 10, "64": 11, "APS (VBR)": 12, "APX (VBR)": 13,
               "q8.x (VBR)": 14, "Other": 15},
    release_types={"Album": 1, "Soundtrack": 3, "EP": 5, "Anthology": 6, "Compilation": 7, "Sampler": 8,
                   "Single": 9, "Demo": 10, "Live album": 11, "Split": 12, "Remix": 13, "Bootleg": 14,
                   "Interview": 15, "Mixtape": 16, "DJ Mix": 17, "Concert recording": 18, "Unknown": 21},
    formats_strict_param="formats_strict",
    media_strict_param="media_strict",
    encodings_strict_param="bitrates_strict",
    categories=dict(zip(CATEGORIES, range(len(CATEGORIES)), strict=True)),
    category_style="listed",
    encodings_label="Encoding",
    group_order=("release_types", "media", "formats", "encodings"),
    defaults={
        "categories": ("Music",),
        "release_types": ALL,
        "media": ("WEB",),
        "formats": ("MP3", "FLAC"),
        "encodings": ("Lossless", "24bit Lossless", "V0 (VBR)", "320"),
    },
    supports_bounty=True,
)

BY_TRACKER: dict[str, TrackerFilters] = {"RED": RED, "OPS": OPS}
"""Only the two transcribed from a live page. DIC is deliberately absent: it is
Gazelle too, but which Gazelle's IDs it inherited is not something to guess at
when guessing wrong silently searches for the wrong thing."""


def for_tracker(tracker: str) -> TrackerFilters | None:
    """The filter vocabulary for a tracker, or None if it is not mapped."""
    return BY_TRACKER.get(tracker.upper())


def _checked(spec: TrackerFilters, key: str, options: list[str], fallback: bool) -> list[str]:
    """Which of a group's boxes start ticked.

    Args:
        spec: The tracker's vocabulary.
        key: The group, e.g. ``"formats"``.
        options: Every option the group offers.
        fallback: What to do when the tracker states no default for this group
            -- tick everything, or nothing.

    Returns:
        The labels to tick, in the group's own order.
    """
    wanted = spec.defaults.get(key)
    if wanted is None:
        return list(options) if fallback else []
    if wanted == ALL:
        return list(options)
    # Intersected with what the group actually offers, in the group's order, so
    # a default naming something a tracker dropped is ignored rather than
    # ticking a box that is not there.
    return [option for option in options if option in set(wanted)]


def schema(tracker: str) -> dict[str, Any]:
    """Describe the filters a tracker takes, for the UI to render itself from.

    Args:
        tracker: Tracker code.

    Returns:
        The available fields and their options. ``mapped`` is False when this
        tracker has no transcribed vocabulary, in which case only the filters
        that need no IDs are offered.
    """
    spec = for_tracker(tracker)
    if spec is None:
        return {
            "tracker": tracker,
            "mapped": False,
            "note": (
                f"{tracker}'s filter IDs have not been verified against its own search page, so only the "
                f"filters that need no IDs are offered. The rest would risk searching for the wrong thing."
            ),
            "form": [
                {"kind": "search"},
                {"kind": "tags"},
                {"kind": "toggle", "key": "show_filled", "label": "Include filled", "default": False},
            ],
            "categories": [],
            "formats": [],
            "media": [],
            "encodings": [],
            "release_types": [],
            "bounty": False,
            "include_old": False,
            "descriptions": False,
            "page_size": PAGE_SIZE,
        }
    groups = {
        "release_types": ("Release types", list(spec.release_types), ""),
        "formats": ("Formats", list(spec.formats), "strict-format"),
        "encodings": (spec.encodings_label, list(spec.encodings), "strict-encoding"),
        "media": ("Media", list(spec.media), "strict-media"),
    }

    # The form, in the order the site lays it out. The page renders this rather
    # than deciding for itself, because the two sites disagree about the order
    # and about what the encoding group is called, and that knowledge belongs
    # in the one module whose job is the difference between them.
    form: list[dict[str, Any]] = [
        {"kind": "search"},
        {"kind": "tags"},
        {
            "kind": "toggle",
            "key": "show_filled",
            "label": "Include filled",
            "default": "show_filled" in spec.toggle_defaults,
        },
    ]
    if spec.supports_include_old:
        form.append({
            "kind": "toggle",
            "key": "include_old",
            "label": "Include old",
            "default": "include_old" in spec.toggle_defaults,
        })
    # OPS asks for the bounty range before the categories; RED has no bounty.
    if spec.supports_bounty:
        form.append({"kind": "bounty", "label": "Bounty offered (GiB)"})
    form.append({
        "kind": "group",
        "key": "categories",
        "label": "Categories",
        "options": list(spec.categories),
        "all": spec.categories_all,
        "checked": _checked(spec, "categories", list(spec.categories), spec.categories_default),
        "strict": "",
    })
    for key in spec.group_order:
        label, options, strict = groups[key]
        form.append({
            "kind": "group",
            "key": key,
            "label": label,
            "options": options,
            "all": True,
            "checked": _checked(spec, key, options, True),
            "strict": strict,
        })

    return {
        "tracker": tracker,
        "mapped": True,
        "note": "",
        "form": form,
        "categories": list(spec.categories),
        "formats": list(spec.formats),
        "media": list(spec.media),
        "encodings": list(spec.encodings),
        "release_types": list(spec.release_types),
        "bounty": spec.supports_bounty,
        "include_old": spec.supports_include_old,
        "descriptions": spec.supports_descriptions,
        "page_size": PAGE_SIZE,
    }


def build_params(
    tracker: str,
    *,
    page: int = 1,
    search: str = "",
    tags: str = "",
    tags_all: bool = False,
    show_filled: bool = False,
    include_old: bool = False,
    search_descriptions: bool = False,
    formats: list[str] | None = None,
    media: list[str] | None = None,
    encodings: list[str] | None = None,
    release_types: list[str] | None = None,
    strict_formats: bool = False,
    strict_media: bool = False,
    strict_encodings: bool = False,
    bounty_min: str = "",
    bounty_max: str = "",
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """Turn a set of choices into one tracker's query parameters.

    Selections are given by label. A label this tracker does not have is
    dropped rather than translated to a number that means something else there.

    "Only specified" is per group on the tracker and off by default, and it has
    to stay that way here. A request states what the requester will accept, so a
    request that accepts *any* media carries no media at all -- turning strict on
    for media hides every one of those. On a real OPS search the same ticks
    return 48 requests with it on and 413 with it off.

    Args:
        tracker: Tracker code.
        page: 1-based page.
        search: Search string.
        tags: Comma-separated tags.
        tags_all: Require every tag rather than any.
        show_filled: Include filled requests.
        include_old: Include old requests. RED only.
        search_descriptions: Search descriptions and comments too. RED only.
        formats: Format labels, e.g. ``["FLAC"]``.
        media: Media labels, e.g. ``["WEB"]``.
        encodings: Encoding labels, e.g. ``["Lossless"]``.
        release_types: Release type labels.
        strict_formats: Exclude requests that name no format at all.
        strict_media: Exclude requests that name no media at all.
        strict_encodings: Exclude requests that name no encoding at all.
        bounty_min: Minimum bounty, in GiB, with an optional M or T suffix. OPS only.
        bounty_max: Maximum bounty. OPS only.
        categories: Category labels, e.g. ``["Music"]``. Empty means every
            category, which is what the form does when none are ticked -- it
            was pinned to Music alone before, so a search here could never
            return the audiobook and application requests the site returns.

    Returns:
        Query parameters for the tracker's ``requests`` action.
    """
    # show_filled is a checkbox on both sites, and a checkbox sends nothing at
    # all when it is unticked. Sending show_filled=false is not "no": PHP reads
    # any non-empty string as true, so the string "false" asks for exactly what
    # it appears to refuse. That single word is why an OPS fetch came back 73%
    # already-filled -- the tracker was not ignoring the parameter, it was
    # obeying it. Omitted for no, "on" for yes, which is what the form does.
    params: dict[str, Any] = {"page": page}
    if show_filled:
        params["show_filled"] = "on"
    if search:
        params["search"] = search

    spec = for_tracker(tracker)

    # The tag mode goes out whether or not there are tags, because that is what
    # the form does: it is a radio pair, and a radio pair always submits. With
    # the box empty the tracker ignores it, so this costs nothing and keeps
    # every search lox makes identical to the same search made on the site.
    if tags:
        params["tags"] = tags
    if spec is not None:
        params[spec.tag_mode_param] = spec.tag_mode_all if tags_all else spec.tag_mode_any
    else:
        # Unmapped: send both spellings. Gazelle ignores what it does not
        # know, and the wrong one here narrows nothing rather than lying.
        params["tags_type"] = 1 if tags_all else 0
        params["tag_mode"] = "all" if tags_all else "any"

    if spec is None:
        return params

    def selected(labels: list[str] | None, table: dict[str, int]) -> list[int]:
        return [table[label] for label in (labels or []) if label in table]

    for values, table, key, strict_param, strict in (
        (formats, spec.formats, "formats[]", spec.formats_strict_param, strict_formats),
        (media, spec.media, "media[]", spec.media_strict_param, strict_media),
        (encodings, spec.encodings, "bitrates[]", spec.encodings_strict_param, strict_encodings),
        (release_types, spec.release_types, "releases[]", "", False),
    ):
        ids = selected(values, table)
        if not ids:
            continue
        params[key] = ids
        if strict_param and strict:
            params[strict_param] = "on"

    if include_old and spec.supports_include_old:
        params["showall"] = "on"
    if search_descriptions and spec.supports_descriptions:
        params["include_descriptions"] = "on"
    if spec.supports_bounty:
        if bounty_min:
            params["bounty_min"] = bounty_min
        if bounty_max:
            params["bounty_max"] = bounty_max

    # None ticked means every category, which is the form's own behaviour: it
    # sends no filter_cat at all and the tracker returns everything.
    wanted = [spec.categories[label] for label in (categories or []) if label in spec.categories]
    if wanted:
        if spec.category_style == "indexed":
            for index in wanted:
                params[f"filter_cat[{index}]"] = 1
        else:
            params["filter_cat[]"] = wanted

    return params
