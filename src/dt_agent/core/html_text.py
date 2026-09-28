"""HTML to plain text conversion for the LLM (D-56, D-44).

An HTML email body is converted to plain text before any LLM sees it:
`<script>`/`<style>`/`<head>` content and images are dropped, block-level
tags become newlines, and each link is kept as its visible text followed
by its href in parentheses — readable prose for a human, and a format
`core/facts.py`'s link checks can parse back out, without ever fetching
the link (D-44: "links analysed as text only, never fetched").
"""

from html.parser import HTMLParser

# Tags whose content (and, for _SKIP_TAGS, descendants) is dropped entirely.
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})

# Tags that introduce a line break in the flattened text.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "ul",
        "ol",
        "section",
        "article",
        "header",
        "footer",
    }
)


class _TextExtractor(HTMLParser):
    """Accumulates visible text, folding links into `text (href)`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._link_href: str | None = None
        self._link_text_start = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            self._link_href = next(
                (value for name, value in attrs if name == "href" and value), None
            )
            self._link_text_start = len(self._chunks)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a":
            if self._link_href is not None:
                text = "".join(self._chunks[self._link_text_start :]).strip()
                del self._chunks[self._link_text_start :]
                folded = f"{text} ({self._link_href})" if text else f"({self._link_href})"
                self._chunks.append(folded)
            self._link_href = None
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Flatten `html` to plain text (D-56).

    Drops `<script>`/`<style>`/`<head>` content and images; keeps link
    text plus its href (`text (href)`); block-level tags become newlines.
    Blank lines are collapsed and outer blank lines trimmed.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    lines: list[str] = []
    for raw_line in parser.text().splitlines():
        line = raw_line.strip()
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)
