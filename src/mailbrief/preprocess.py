"""Mail body cleanup before the LLM call: HTML -> text, remove quoted history and signatures."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(src: str) -> str:
    if "<" not in src or ">" not in src:
        return html.unescape(src)
    p = _TextExtractor()
    p.feed(src)
    return "".join(p.parts)


# Markers that start the quoted part of a reply/forward (English + Korean Outlook).
_QUOTE_MARKERS = [
    r"^-{2,}\s*Original Message\s*-{2,}",
    r"^-{2,}\s*원본 메시지\s*-{2,}",
    r"^_{10,}\s*$",
    r"^From:\s.+",
    r"^보낸 사람:\s*.+",
    r"^On .+ wrote:\s*$",
    r"^\d{4}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2}.*작성:\s*$",
]
_QUOTE_RE = re.compile("|".join(f"(?:{m})" for m in _QUOTE_MARKERS), re.IGNORECASE)
_SIG_RE = re.compile(r"^(--\s*|Best regards,?|Regards,?|감사합니다\.?|Sent from my .+)$", re.IGNORECASE)


def strip_quoted(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        # Keep "From:" only when it is part of the first lines (forward header of the mail itself is rare).
        if i > 0 and _QUOTE_RE.match(s):
            break
        if s.startswith(">"):
            continue
        out.append(line)
    # Cut the signature: first signature marker in the last 15 lines.
    start = max(0, len(out) - 15)
    for j in range(start, len(out)):
        if _SIG_RE.match(out[j].strip()):
            # keep the closing word itself when it is "감사합니다" (often the only content line)
            out = out[: j + 1] if out[j].strip().startswith("감사") else out[:j]
            break
    return "\n".join(out)


def clean_body(raw: str, max_chars: int = 4000) -> str:
    text = html_to_text(raw)
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = strip_quoted(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text
