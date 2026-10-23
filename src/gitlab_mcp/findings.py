"""Conservative compaction for bot-authored review findings."""

import re
from itertools import pairwise

_PARAGRAPH_BREAK = re.compile(r"(?:\r?\n[ \t]*){2,}")
_FENCE_START = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_ORDERED_ITEM = re.compile(r"^[ \t]{0,3}\d{1,3}[.)][ \t]+\S")
_MARKDOWN_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_BOLD_HEADING = re.compile(
    r"^(?:#{1,6}[ \t]+)?\*\*(.+?)\*\*(?:[ \t]+[\U0001F000-\U0001FAFF\u2600-\u27BF]\ufe0f?)*[ \t]*$",
)
_DASH_BULLET = re.compile(r"^[ \t]{0,3}-[ \t]+\S")
_FIX_LABEL = re.compile(r"^[ \t]{0,3}(?:-[ \t]+)?(?:\*\*)?fix\s*:", re.IGNORECASE)
_PATH_LINE = re.compile(r"(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+:\d+\b")
_GREETING = re.compile(
    r"^(?:👋[ \t]*)?(?:hi|hello|hey)(?:[ \t]+@[\w.-]+)?[!,.]?$",
    re.IGNORECASE,
)
_SIGNOFF = re.compile(
    r"^(?:(?:thanks(?: so much)?|thank you(?: so much)?|best(?: regards)?|kind regards|regards|sincerely|cheers)"
    r"[!,.]?|full steam ahead! 💨)$",
    re.IGNORECASE,
)
_SECTION_SIGNOFF = re.compile(r"^(?:[\w' ,.-]+ )?ready to leave the station! 🛤️$", re.IGNORECASE)
_PEEP_GREETING = re.compile(
    r"^Peep peep![ \t]*(?:🚂[ \t]*)?(?:\.\.\.[ \t]*)?(?:Really useful work![ \t]*⭐)?$",
)
_RETAINED_SECTIONS = {
    "needs to be improved",
    "what needs to be improved",
    "failure scenario",
    "how to fix",
}
_REMOVED_SECTIONS = {"what was done well"}
_KNOWN_SECTIONS = _RETAINED_SECTIONS | _REMOVED_SECTIONS


def compact_finding_body(body: str) -> str:
    """Compact recognized bot finding formats without changing ambiguous note content.

    Numbered findings retain their existing conservative behavior. Explicit section
    findings keep only approved sections, and a narrow bold-heading bullet layout
    drops its introductory paragraph. Unrecognized bodies are returned unchanged.
    """
    spans, separators = _paragraph_spans(body)
    finding_paragraphs, code_paragraphs = _finding_structure(body, spans)
    section_body = _compact_section_format(body, spans, code_paragraphs, separators)
    if section_body is not None:
        return section_body

    if finding_paragraphs:
        return _compact_numbered_findings(body, spans, separators, finding_paragraphs, code_paragraphs)

    bullet_body = _compact_bullet_format(body, spans, code_paragraphs, separators)
    return bullet_body if bullet_body is not None else body


def _compact_numbered_findings(
    body: str,
    spans: list[tuple[int, int]],
    separators: list[tuple[int, int]],
    finding_paragraphs: set[int],
    code_paragraphs: set[int],
) -> str:
    """Remove only separate greetings and signoffs around numbered findings."""
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


def _compact_section_format(
    body: str,
    spans: list[tuple[int, int]],
    code_paragraphs: set[int],
    separators: list[tuple[int, int]],
) -> str | None:
    """Keep a bold finding title and its recognized descriptive sections."""
    heading_index = _finding_heading_index(body, spans, code_paragraphs)
    if heading_index is None:
        return None

    section_headings = {
        index: _section_heading(body[slice(*span)])
        for index, span in enumerate(spans)
        if index > heading_index and index not in code_paragraphs
    }
    if not any(heading in _RETAINED_SECTIONS for heading in section_headings.values()):
        return None

    retained = {heading_index}
    current_section = ""
    for index in range(heading_index + 1, len(spans)):
        section = section_headings.get(index)
        if section in _KNOWN_SECTIONS:
            current_section = section
            if section in _RETAINED_SECTIONS:
                retained.add(index)
        elif current_section in _RETAINED_SECTIONS:
            retained.add(index)

    trailing_index = max(retained)
    while trailing_index > heading_index:
        paragraph = body[slice(*spans[trailing_index])].strip()
        if trailing_index in code_paragraphs or not (
            _SIGNOFF.fullmatch(paragraph) or _SECTION_SIGNOFF.fullmatch(paragraph)
        ):
            break
        retained.discard(trailing_index)
        trailing_index -= 1

    return _join_paragraphs(body, spans, separators, retained)


def _compact_bullet_format(
    body: str,
    spans: list[tuple[int, int]],
    code_paragraphs: set[int],
    separators: list[tuple[int, int]],
) -> str | None:
    """Remove the introductory paragraph from a recognized bold-heading action list."""
    heading_index = _finding_heading_index(body, spans, code_paragraphs)
    if heading_index is None or heading_index + 1 >= len(spans):
        return None

    first_paragraph = heading_index + 1
    action_paragraphs = {
        index
        for index in range(first_paragraph, len(spans))
        if index not in code_paragraphs and _has_action_marker(body[slice(*spans[index])])
    }
    if not action_paragraphs or first_paragraph in action_paragraphs or first_paragraph in code_paragraphs:
        return None

    retained = set(range(len(spans))) - {first_paragraph}
    for index in range(heading_index):
        paragraph = body[slice(*spans[index])].strip()
        if index not in code_paragraphs and _is_greeting_paragraph(paragraph):
            retained.discard(index)

    trailing_index = len(spans) - 1
    last_action = max(action_paragraphs)
    while trailing_index > last_action:
        paragraph = body[slice(*spans[trailing_index])].strip()
        if trailing_index in code_paragraphs or not _SIGNOFF.fullmatch(paragraph):
            break
        retained.discard(trailing_index)
        trailing_index -= 1

    return _join_paragraphs(body, spans, separators, retained)


def _finding_heading_index(body: str, spans: list[tuple[int, int]], code_paragraphs: set[int]) -> int | None:
    """Find the first standalone bold title paragraph outside code."""
    for index, span in enumerate(spans):
        if index in code_paragraphs:
            continue
        paragraph = body[slice(*span)].strip()
        heading = _BOLD_HEADING.fullmatch(paragraph)
        if heading and _normalized_heading(heading.group(1)) not in _KNOWN_SECTIONS:
            return index
    return None


def _section_heading(paragraph: str) -> str | None:
    """Return a known section name when a paragraph is a standalone heading."""
    lines = paragraph.strip().splitlines()
    if not lines:
        return None
    text = lines[0].strip()

    markdown_heading = _MARKDOWN_HEADING.fullmatch(text)
    bold_heading = _BOLD_HEADING.fullmatch(text)
    if markdown_heading:
        text = markdown_heading.group(1)
    elif bold_heading:
        text = bold_heading.group(1)
    elif _normalized_heading(text) not in _KNOWN_SECTIONS:
        return None

    normalized = _normalized_heading(text)
    return normalized if normalized in _KNOWN_SECTIONS else None


def _normalized_heading(text: str) -> str:
    """Normalize heading text by ignoring case, emoji, and punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _has_action_marker(paragraph: str) -> bool:
    """Check whether a paragraph contains a dash action, a Fix label, or a path and line."""
    for line in paragraph.splitlines():
        if _DASH_BULLET.match(line) or _FIX_LABEL.match(line) or _PATH_LINE.search(line):
            return True
    return False


def _join_paragraphs(
    body: str,
    spans: list[tuple[int, int]],
    separators: list[tuple[int, int]],
    paragraph_indices: set[int],
) -> str:
    """Join selected paragraph slices while preserving source separators when adjacent."""
    selected = sorted(paragraph_indices)
    if not selected:
        return ""

    default_separator = body[slice(*separators[0])] if separators else "\n\n"
    chunks = [body[slice(*spans[selected[0]])]]
    for previous, current in pairwise(selected):
        separator = body[slice(*separators[previous])] if current == previous + 1 else default_separator
        chunks.extend((separator, body[slice(*spans[current])]))
    return "".join(chunks)


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
