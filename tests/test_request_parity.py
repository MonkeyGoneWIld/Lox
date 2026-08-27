"""A search here has to be the same search the tracker's own form makes.

The request page is a copy of two forms that look identical and are not, and
the only way to know lox is asking the same question is to ask it beside a
real one. ``tests/data/request_search_urls.txt`` holds URLs captured off the
live RED and OPS search forms with known boxes ticked. Each is turned back into
the labels a user would have picked, run through build_params, and compared
with the URL it came from.

What this caught, and what would otherwise still be silently wrong:

  * every search was pinned to the Music category and there was no way to pick
    another, so the audiobook, application and e-book requests the site returns
    could not appear here however the page was set
  * RED numbers its categories from 1 and indexes them (``filter_cat[3]=1``)
    while OPS numbers from 0 and lists them (``filter_cat[]=2``) -- the same
    seven labels in the same order, filed differently
  * the tag mode was only sent when there were tags, where the form sends it
    always, because it is a radio pair and a radio pair always submits

The ID tables came out of this unchanged, which is the other half of the
result: releases, formats, bitrates and media were already right on both.
"""

import os
import sys
from urllib.parse import parse_qsl, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("LOX_HOST", "127.0.0.1")
os.environ.setdefault("LOX_PORT", "5015")
os.environ.setdefault("LOX_AUTH_TOKEN", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("LOX_DOWNLOAD_DIR", os.path.join(ROOT, "_parity", "downloads"))
os.environ.setdefault("LOX_TORRENTS_DIR", os.path.join(ROOT, "_parity", "torrents"))
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox.checker.request_filters import CATEGORIES, build_params, for_tracker, schema  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def parsed(url: str) -> dict:
    """The URL's query, with the ``name[]`` arrays collected into lists."""
    out: dict = {}
    for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if key.endswith("[]"):
            out.setdefault(key, []).append(value)
        else:
            out[key] = value
    return out


def labels_for(ids: list[str], table: dict[str, int]) -> list[str]:
    """Reverse tracker ids back to the labels someone ticked."""
    back = {str(v): k for k, v in table.items()}
    return [back[i] for i in ids if i in back]


def categories_from(query: dict, tracker: str) -> list[str]:
    """Which categories the captured URL asked for."""
    if tracker == "OPS":
        return [CATEGORIES[int(i)] for i in query.get("filter_cat[]", []) if int(i) < len(CATEGORIES)]
    picked = []
    for key in query:
        if key.startswith("filter_cat[") and key != "filter_cat[]":
            index = int(key[len("filter_cat["):-1])
            if 1 <= index <= len(CATEGORIES):
                picked.append(CATEGORIES[index - 1])
    return picked


def comparable(params: dict) -> dict:
    """Strings, sorted arrays, and without the parts that are not a filter."""
    out = {}
    for key, value in params.items():
        if key in ("page", "submit"):
            continue
        out[key] = sorted(str(v) for v in value) if isinstance(value, list) else str(value)
    # An empty box on the form and lox leaving it out are the same request.
    return {k: v for k, v in out.items() if v not in ("", [])}


def main() -> int:
    path = os.path.join(ROOT, "data", "request_search_urls.txt")
    with open(path, encoding="utf-8") as handle:
        cases = [line for line in handle.read().splitlines() if line.strip()]
    check("there are captured searches to compare against", len(cases) >= 7, str(len(cases)))

    for line in cases:
        tracker, name, url = line.split("|", 2)
        spec = for_tracker(tracker)
        want = parsed(url)

        built = build_params(
            tracker,
            page=1,
            search=want.get("search", ""),
            tags=want.get("tags", ""),
            tags_all=(want.get("tags_type") == "1") or (want.get("tag_mode") == "all"),
            show_filled="show_filled" in want,
            include_old="showall" in want,
            search_descriptions="include_descriptions" in want,
            formats=labels_for(want.get("formats[]", []), spec.formats),
            media=labels_for(want.get("media[]", []), spec.media),
            encodings=labels_for(want.get("bitrates[]", []), spec.encodings),
            release_types=labels_for(want.get("releases[]", []), spec.release_types),
            strict_formats="formats_strict" in want,
            strict_media="media_strict" in want,
            strict_encodings=("bitrate_strict" in want) or ("bitrates_strict" in want),
            bounty_min=want.get("bounty_min", ""),
            bounty_max=want.get("bounty_max", ""),
            categories=categories_from(want, tracker),
        )

        site, lox = comparable(want), comparable(built)
        missing = sorted(k for k in site if k not in lox)
        extra = sorted(k for k in lox if k not in site)
        differs = sorted(k for k in site if k in lox and site[k] != lox[k])
        detail = ""
        if missing:
            detail += f" never sent: {missing}"
        if extra:
            detail += f" sent anyway: {extra}"
        if differs:
            detail += f" differs: {[(k, site[k], lox[k]) for k in differs]}"
        check(f"{tracker} / {name} asks the tracker exactly what the form asks",
              not (missing or extra or differs), detail.strip())

    # --- the two numbering schemes, stated rather than assumed -------------
    red, ops = for_tracker("RED"), for_tracker("OPS")
    check("both sites offer the same seven categories in the same order",
          list(red.categories) == list(ops.categories) == list(CATEGORIES), str(list(red.categories)))
    check("RED numbers them from 1", red.categories["Music"] == 1 and red.categories["Comics"] == 7, "")
    check("OPS numbers them from 0", ops.categories["Music"] == 0 and ops.categories["Comics"] == 6, "")
    check("and they are filed differently",
          red.category_style == "indexed" and ops.category_style == "listed", "")

    # A category nobody ticked means every category, which is what the form
    # does -- it sends no filter_cat at all. Sending one would narrow it.
    for tracker in ("RED", "OPS"):
        params = build_params(tracker, categories=[])
        check(f"{tracker}: no category ticked filters no category",
              not any(k.startswith("filter_cat") for k in params), str(sorted(params)))

    # --- the page can render every filter the tracker takes ----------------
    for tracker in ("RED", "OPS"):
        spec = schema(tracker)
        check(f"{tracker} offers its categories to the page",
              len(spec["categories"]) == 7, str(len(spec["categories"])))
    check("RED offers include-old and descriptions, OPS does not",
          schema("RED")["include_old"] and schema("RED")["descriptions"]
          and not schema("OPS")["include_old"] and not schema("OPS")["descriptions"], "")
    check("OPS offers a bounty range, RED does not",
          schema("OPS")["bounty"] and not schema("RED")["bounty"], "")

    # --- the form is laid out the way the site lays it out -----------------
    # The page renders whatever this describes, so the order, the labels and
    # the defaults are all assertable here rather than only visible on screen.
    def rows(tracker: str) -> list[dict]:
        return schema(tracker)["form"]

    def labels(tracker: str) -> list[str]:
        return [r.get("label", r["kind"]) for r in rows(tracker)]

    def group(tracker: str, key: str) -> dict:
        return next(r for r in rows(tracker) if r.get("key") == key)

    check("RED orders its groups the way RED's form does",
          [r["key"] for r in rows("RED") if r["kind"] == "group"]
          == ["categories", "release_types", "formats", "encodings", "media"],
          str(labels("RED")))
    check("OPS orders its groups the way OPS's form does",
          [r["key"] for r in rows("OPS") if r["kind"] == "group"]
          == ["categories", "release_types", "media", "formats", "encodings"],
          str(labels("OPS")))

    # Same group, two names. Showing one site's word while searching the other
    # is showing a label that is not on the form being copied.
    check("RED calls the encoding group Bitrates",
          group("RED", "encodings")["label"] == "Bitrates", "")
    check("OPS calls it Encoding",
          group("OPS", "encodings")["label"] == "Encoding", "")

    # RED has no All over its categories and starts them clear, because clear
    # is how you ask RED for all of them. OPS has one and starts them ticked.
    red_cats, ops_cats = group("RED", "categories"), group("OPS", "categories")
    check("RED's categories have no All and start clear",
          not red_cats["all"] and not red_cats["default"], "")
    check("OPS's categories have an All and start ticked",
          ops_cats["all"] and ops_cats["default"], "")

    # An unnarrowed search is a search for everything, which is how both forms
    # arrive -- every box already ticked.
    for tracker in ("RED", "OPS"):
        others = [r for r in rows(tracker) if r["kind"] == "group" and r["key"] != "categories"]
        check(f"{tracker}'s other groups all start ticked",
              others and all(r["all"] and r["default"] for r in others),
              str([r["label"] for r in others]))

    # OPS asks for the bounty before the categories; RED never asks.
    check("OPS puts the bounty where its form puts it",
          labels("OPS").index("Bounty offered (GiB)") < labels("OPS").index("Categories"), "")
    check("a tracker with no filters still gets a usable form",
          len(schema("SOMETHINGELSE")["form"]) == 3, "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
