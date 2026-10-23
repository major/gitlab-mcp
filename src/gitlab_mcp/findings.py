"""Conservative compaction for bot-authored review findings."""

import re

_PARAGRAPH_BREAK = re.compile(r"(?:\r?\n[ \t]*){2,}")
_FENCE_START = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_ORDERED_ITEM = re.compile(r"^[ \t]{0,3}\d{1,3}[.)][ \t]+\S")
_GREETING = re.compile(
    r"^(?:👋[ \t]*)?(?:hi|hello|hey)(?:[ \t]+@[\w.-]+)?[!,.]?$",
    re.IGNORECASE,
)
_SIGNOFF = re.compile(
    r"^(?:(?:thanks(?: so much)?|thank you(?: so much)?|best(?: regards)?|kind regards|regards|sincerely|cheers)"
    r"[!,.]?|full steam ahead! 💨)$",
    re.IGNORECASE,
)
_PEEP_GREETING = re.compile(
    r"^Peep peep![ \t]*(?:🚂[ \t]*)?(?:\.\.\.[ \t]*)?(?:Really useful work![ \t]*⭐)?$",
)


def compact_finding_body(body: str) -> str:
    """Remove only separate greetings and signoffs around a bot's numbered findings.

    Bodies without an ordered finding are returned unchanged. Other paragraphs,
    including headings, locations, fixes, and fenced code, are never discarded.
    """
    spans, separators = _paragraph_spans(body)
    finding_paragraphs, code_paragraphs = _finding_structure(body, spans)
    if not finding_paragraphs:
        return body

    first_finding = min(finding_paragraphs)
    last_finding = max(finding_paragraphs)
    removals: list[tuple[int, int]] = []

    for index in range(first_finding):
        paragraph = body[slice(*spans[index])].strip()
        if index not in code_paragraphs and _is_greeting_paragraph(paragraph):
            # Include the following paragraph separator, but keep the prior heading if present.
            end = spans[index + 1][0] if index + 1 < len(spans) else spans[index][1]
            removals.append((spans[index][0], end))

    trailing_index = len(spans) - 1
    while trailing_index > last_finding and not body[slice(*spans[trailing_index])].strip():
        trailing_index -= 1
    while trailing_index > last_finding:
        paragraph = body[slice(*spans[trailing_index])].strip()
        if trailing_index in code_paragraphs or not _SIGNOFF.fullmatch(paragraph):
            break
        # Include the separator before each trailing signoff and its trailing whitespace.
        start = separators[trailing_index - 1][0] if trailing_index > 0 else spans[trailing_index][0]
        removals.append((start, len(body)))
        trailing_index -= 1

    if not removals:
        return body

    removals.sort()
    compacted: list[str] = []
    cursor = 0
    for start, end in removals:
        if start > cursor:
            compacted.append(body[cursor:start])
        cursor = max(cursor, end)
    compacted.append(body[cursor:])
    return "".join(compacted)


def _is_greeting_paragraph(paragraph: str) -> bool:
    """Match only known standalone greeting text."""
    return bool(_GREETING.fullmatch(paragraph) or _PEEP_GREETING.fullmatch(paragraph))


def _paragraph_spans(body: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Return paragraph and separator offsets without normalizing the source text."""
    separators = [(match.start(), match.end()) for match in _PARAGRAPH_BREAK.finditer(body)]
    spans: list[tuple[int, int]] = []
    start = 0
    for separator_start, separator_end in separators:
        spans.append((start, separator_start))
        start = separator_end
    spans.append((start, len(body)))
    return spans, separators


def _finding_structure(body: str, spans: list[tuple[int, int]]) -> tuple[set[int], set[int]]:
    """Find ordered items and code paragraphs without treating code as prose."""
    findings: set[int] = set()
    code_paragraphs: set[int] = set()
    fence_char = ""
    fence_length = 0
    offset = 0
    paragraph_index = 0

    for line in body.splitlines(keepends=True):
        while paragraph_index + 1 < len(spans) and offset >= spans[paragraph_index + 1][0]:
            paragraph_index += 1
        content = line.rstrip("\r\n")
        if content.startswith(("    ", "\t")):
            # Markdown indentation is ambiguous, so preserve the whole paragraph.
            code_paragraph_index = paragraph_index
            content_offset = offset + len(content) - len(content.lstrip(" \t"))
            while code_paragraph_index + 1 < len(spans) and content_offset >= spans[code_paragraph_index + 1][0]:
                code_paragraph_index += 1
            code_paragraphs.add(code_paragraph_index)
        fence = _FENCE_START.match(content)
        if fence_char:
            code_paragraphs.add(paragraph_index)
            closing = re.match(rf"^[ \t]{{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*$", content)
            if closing:
                fence_char = ""
                fence_length = 0
        elif fence:
            code_paragraphs.add(paragraph_index)
            fence_token = fence.group(1)
            fence_char = fence_token[0]
            fence_length = len(fence_token)
        elif _ORDERED_ITEM.match(content):
            findings.add(paragraph_index)
        offset += len(line)

    return findings, code_paragraphs
