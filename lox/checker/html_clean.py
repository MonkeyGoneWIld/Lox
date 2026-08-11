"""Cut tracker-supplied HTML down to a set of tags that are safe to insert.

The trackers do not agree on what a description is. RED renders it before
sending it, so ``description`` arrives as HTML and the BBCode comes alongside
under ``bbDescription``; other responses carry the BBCode in ``description``
itself. Escaping HTML shows the reader the markup instead of the link, and
inserting it unread would run whatever the person who wrote the request put
there -- so it goes through here.

Allow-list only: a tag not named below is dropped, an attribute not named below
is dropped, and the text inside is kept either way. Script, style and the
embedding tags lose their contents too, since that content is not prose.
"""

import re
from html import escape
from html.parser import HTMLParser

__all__ = ["sanitize", "looks_like_html"]

_VOID = {"br", "hr", "img", "wbr"}

_ALLOWED: dict[str, set[str]] = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "b": set(), "strong": set(), "i": set(), "em": set(), "u": set(), "s": set(), "strike": set(),
    "sub": set(), "sup": set(), "small": set(), "big": set(), "tt": set(), "var": set(), "kbd": set(),
    "p": set(), "div": set(), "span": set(), "br": set(), "hr": set(),
    "blockquote": set(), "cite": set(), "q": set(),
    "pre": set(), "code": set(),
    "ul": set(), "ol": {"start"}, "li": set(), "dl": set(), "dt": set(), "dd": set(),
    "h1": set(), "h2": set(), "h3": set(), "h4": set(), "h5": set(), "h6": set(),
    "table": set(), "thead": set(), "tbody": set(), "tfoot": set(),
    "tr": set(), "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"},
    "img": {"src", "alt", "title"},
    "details": set(), "summary": set(),
    "font": set(),
}

# Dropped along with everything inside them: none of it is text to read.
_DISCARD = {"script", "style", "iframe", "object", "embed", "form", "input", "button",
            "textarea", "select", "option", "noscript", "svg", "math", "link", "meta"}

_URL_OK = re.compile(r"^(https?:|mailto:|/|#)", re.IGNORECASE)

# Style is kept only for the handful of declarations a description actually
# uses, and only with values that cannot carry a url() or an expression.
_STYLE_PROPS = {"color", "background-color", "font-size", "font-weight", "font-style",
                "text-decoration", "text-align"}
_STYLE_VALUE = re.compile(r"^[#\w\s.,%()-]{1,60}$")


def _clean_style(value: str) -> str:
    kept = []
    for part in value.split(";"):
        name, _, val = part.partition(":")
        name, val = name.strip().lower(), val.strip()
        if name in _STYLE_PROPS and val and _STYLE_VALUE.match(val) and "url" not in val.lower():
            kept.append(f"{name}: {val}")
    return "; ".join(kept)


class _Cleaner(HTMLParser):
    """Rebuilds the document, keeping only what is on the list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.muted = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _DISCARD:
            self.muted += 1
            return
        if self.muted or tag not in _ALLOWED:
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        if tag not in _VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self.muted or tag in _DISCARD or tag not in _ALLOWED:
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        if tag not in _VOID:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DISCARD:
            self.muted = max(0, self.muted - 1)
            return
        if self.muted or tag in _VOID or tag not in _ALLOWED:
            return
        # Close only a tag actually open, so stray end tags cannot unbalance
        # the surrounding page.
        if tag in self.open:
            while self.open:
                current = self.open.pop()
                self.out.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self.muted:
            self.out.append(escape(data, quote=False))

    def _attrs(self, tag: str, attrs) -> str:
        allowed = _ALLOWED.get(tag, set())
        parts = []
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name.startswith("on") or value.strip().lower().startswith("javascript:"):
                continue
            if name == "style":
                cleaned = _clean_style(value)
                if cleaned:
                    parts.append(f'style="{escape(cleaned, quote=True)}"')
                continue
            if name not in allowed:
                continue
            if name in ("href", "src") and not _URL_OK.match(value.strip()):
                continue
            parts.append(f'{name}="{escape(value, quote=True)}"')
        if tag == "a":
            parts.append('target="_blank" rel="noopener noreferrer"')
        if tag == "img":
            parts.append('class="bb-img" loading="lazy" referrerpolicy="no-referrer"')
        return (" " + " ".join(parts)) if parts else ""

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def sanitize(text) -> str:
    """Return `text` with every tag not on the allow-list removed.

    Args:
        text: HTML as a tracker sent it.

    Returns:
        HTML safe to insert: no scripts, no event handlers, no framed content,
        no url() in a style, and every link forced to open in a new tab without
        carrying a referrer.
    """
    if not text:
        return ""
    cleaner = _Cleaner()
    cleaner.feed(str(text))
    cleaner.close()
    return cleaner.result()


def looks_like_html(text) -> bool:
    """Whether this is markup the tracker already rendered.

    Used to decide between sanitising and running the BBCode renderer, for the
    responses that do not hand over the BBCode separately.
    """
    if not text:
        return False
    return re.search(r"<(a|br|p|div|span|strong|b|i|em|u|img|blockquote|ul|ol|li|pre|h[1-6])\b[^>]*>",
                     str(text), re.IGNORECASE) is not None
