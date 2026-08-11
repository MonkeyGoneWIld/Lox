"""Turn Gazelle's BBCode into HTML that is safe to put on the page.

Request descriptions and comments are stored as BBCode, and the ajax API hands
them back unrendered -- the tracker's own page is what renders them, and that
page refuses to be embedded. So the rendering happens here.

Everything is escaped first and only a fixed list of tags is turned back into
markup afterwards, so anything unrecognised survives as the literal text it was.
That is the safe failure: an unknown tag looks wrong, it does not execute.
"""

import html
import re
from typing import Any

__all__ = ["render", "strip"]

# Colour and size are attacker-controlled in principle -- they come from a
# description someone else wrote -- so they are matched, not trusted.
_COLOUR = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20})$")
_SIZE_PX = {"1": 10, "2": 12, "3": 14, "4": 17, "5": 20, "6": 24, "7": 28, "8": 32, "9": 36, "10": 42}
_URL_OK = re.compile(r"^https?://[^\s\"'<>]+$", re.IGNORECASE)
_BARE_URL = re.compile(r"(?<![=\"'>\w])(https?://[^\s<>\[\]\"']+)")

_SIMPLE = {
    "b": ("<strong>", "</strong>"),
    "i": ("<em>", "</em>"),
    "u": ("<u>", "</u>"),
    "s": ("<s>", "</s>"),
    "important": ('<div class="bb-important">', "</div>"),
}

_ALIGN = {"left", "right", "center", "centre", "justify"}


def _placeholder(index: int) -> str:
    """A marker no BBCode or escaped HTML can contain."""
    return f"\x00bb{index}\x00"


def _extract_verbatim(text: str, store: list[str]) -> str:
    """Pull [code] and [pre] bodies out so nothing else rewrites them."""

    def take(match: re.Match) -> str:
        tag = match.group(1).lower()
        body = match.group(2)
        store.append(f'<pre class="bb-code">{body}</pre>' if tag in ("code", "pre") else body)
        return _placeholder(len(store) - 1)

    return re.sub(r"\[(code|pre)\](.*?)\[/\1\]", take, text, flags=re.DOTALL | re.IGNORECASE)


def _restore(text: str, store: list[str]) -> str:
    for index, value in enumerate(store):
        text = text.replace(_placeholder(index), value)
    return text


def _apply_pairs(text: str) -> str:
    """Rewrite the paired tags, innermost first, until nothing changes."""
    for _ in range(12):
        before = text
        for tag, (open_html, close_html) in _SIMPLE.items():
            text = re.sub(
                rf"\[{tag}\](.*?)\[/{tag}\]",
                lambda m, o=open_html, c=close_html: f"{o}{m.group(1)}{c}",
                text,
                flags=re.DOTALL | re.IGNORECASE,
            )
        text = re.sub(r"\[color=([^\]]{1,20})\](.*?)\[/color\]", _colour, text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\[size=([^\]]{1,4})\](.*?)\[/size\]", _size, text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\[align=([^\]]{1,10})\](.*?)\[/align\]", _align, text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\[spoiler(?:=[^\]]*)?\](.*?)\[/spoiler\]", _spoiler, text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\[hide(?:=[^\]]*)?\](.*?)\[/hide\]", _spoiler, text, flags=re.DOTALL | re.IGNORECASE)
        if text == before:
            break
    return text


def _colour(match: re.Match) -> str:
    value = match.group(1).strip()
    if not _COLOUR.match(value):
        return match.group(0)
    return f'<span style="color: {html.escape(value, quote=True)}">{match.group(2)}</span>'


def _size(match: re.Match) -> str:
    px = _SIZE_PX.get(match.group(1).strip())
    if not px:
        return match.group(0)
    return f'<span style="font-size: {px}px">{match.group(2)}</span>'


def _align(match: re.Match) -> str:
    value = match.group(1).strip().lower()
    if value not in _ALIGN:
        return match.group(0)
    return f'<div style="text-align: {"center" if value == "centre" else value}">{match.group(2)}</div>'


def _spoiler(match: re.Match) -> str:
    return f"<details class='bb-spoiler'><summary>Spoiler</summary>{match.group(1)}</details>"


def _links(text: str, base_url: str) -> str:
    """[url], [img], and the tracker's own [artist] and [user] shorthands."""

    def url_named(match: re.Match) -> str:
        href = html.unescape(match.group(1).strip())
        if not _URL_OK.match(href):
            return match.group(2)
        safe = html.escape(href, quote=True)
        return f'<a href="{safe}" target="_blank" rel="noopener noreferrer">{match.group(2)}</a>'

    def url_bare(match: re.Match) -> str:
        href = html.unescape(match.group(1).strip())
        if not _URL_OK.match(href):
            return match.group(0)
        safe = html.escape(href, quote=True)
        return f'<a href="{safe}" target="_blank" rel="noopener noreferrer">{html.escape(href)}</a>'

    def image(match: re.Match) -> str:
        src = html.unescape(match.group(1).strip())
        if not _URL_OK.match(src):
            return match.group(0)
        safe = html.escape(src, quote=True)
        return f'<img class="bb-img" src="{safe}" loading="lazy" referrerpolicy="no-referrer" alt="">'

    def tracker_link(path: str):
        def build(match: re.Match) -> str:
            name = match.group(1).strip()
            if not base_url or not name:
                return name
            from urllib.parse import quote as urlquote

            href = f"{base_url}/{path}{urlquote(html.unescape(name))}"
            return f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{name}</a>'

        return build

    text = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", url_named, text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\[url\](.*?)\[/url\]", url_bare, text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\[img\](.*?)\[/img\]", image, text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\[img=([^\]]+)\]", image, text, flags=re.IGNORECASE)
    text = re.sub(r"\[artist\](.*?)\[/artist\]", tracker_link("artist.php?artistname="),
                  text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\[user\](.*?)\[/user\]", tracker_link("user.php?action=search&search="),
                  text, flags=re.DOTALL | re.IGNORECASE)
    return _BARE_URL.sub(url_bare, text)


def _quotes(text: str) -> str:
    """[quote] and [quote=who], innermost first so nesting survives."""
    for _ in range(8):
        before = text
        text = re.sub(
            r"\[quote=([^\]]{1,80})\]((?:(?!\[quote).)*?)\[/quote\]",
            lambda m: f'<blockquote class="bb-quote"><cite>{m.group(1)}</cite>{m.group(2)}</blockquote>',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"\[quote\]((?:(?!\[quote).)*?)\[/quote\]",
            lambda m: f'<blockquote class="bb-quote">{m.group(1)}</blockquote>',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if text == before:
            break
    return text


def _lists(text: str) -> str:
    """[list]...[*]item...[/list], including the numbered form."""

    def build(match: re.Match) -> str:
        ordered = bool(match.group(1))
        items = [part.strip() for part in re.split(r"\[\*\]", match.group(2)) if part.strip()]
        if not items:
            return ""
        tag = "ol" if ordered else "ul"
        body = "".join(f"<li>{item}</li>" for item in items)
        return f'<{tag} class="bb-list">{body}</{tag}>'

    return re.sub(r"\[list(=1)?\](.*?)\[/list\]", build, text, flags=re.DOTALL | re.IGNORECASE)


def render(text: Any, base_url: str = "") -> str:
    """Render BBCode as HTML.

    Args:
        text: The BBCode, as the tracker stored it.
        base_url: The tracker's origin, used for its [artist] and [user] tags.

    Returns:
        HTML safe to insert. Every character of the input is escaped before any
        tag is recognised, so nothing in the source can become live markup.
    """
    if not text:
        return ""
    source = html.escape(html.unescape(str(text)), quote=False)

    verbatim: list[str] = []
    source = _extract_verbatim(source, verbatim)

    source = source.replace("[n]", "")
    source = _apply_pairs(source)
    source = _links(source, base_url.rstrip("/"))
    source = _quotes(source)
    source = _lists(source)
    source = re.sub(r"\[hr\]", "<hr>", source, flags=re.IGNORECASE)
    source = source.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")

    return _restore(source, verbatim)


def strip(text: Any) -> str:
    """The same content with every tag removed, for titles and summaries."""
    if not text:
        return ""
    plain = re.sub(r"\[/?[a-zA-Z][^\]]{0,80}\]", "", html.unescape(str(text)))
    return re.sub(r"\s+", " ", plain).strip()
