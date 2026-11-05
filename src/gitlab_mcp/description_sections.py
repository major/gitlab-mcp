"""Markdown heading sections for focused description reads 📑.

Work items and merge requests share these helpers, so section names match the same way in both tools.
"""

import json
import re
from typing import Any, NoReturn

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

FIND_TERM_CAP = 20
FIND_TERM_LENGTH = 200
FIND_HIT_CAP = 100


class DescriptionSection(BaseModel):
    """One selected Markdown heading and its bounded content."""

    heading: str
    content: str
    truncated: bool = Field(default=False, exclude_if=lambda value: not value)


def _raise_tool_error(message: str) -> NoReturn:
    """Raise one caller-facing argument validation error."""
    raise ToolError(message)


def validate_focus_options(  # noqa: PLR0913 - focused reads share these independent options.
    section: str | list[str] | None,
    headings_only: bool,
    include_description: bool,
    description_limit: int,
    *,
    find: list[str] | None = None,
    find_context: int = 0,
) -> bool:
    """Reject incompatible focused-read options before any request and return whether one is set."""
    if not isinstance(headings_only, bool):
        _raise_tool_error("headings_only must be a boolean.")
    section_selectors(section)
    if section is not None and headings_only:
        _raise_tool_error("section and headings_only cannot be used together.")
    section_focused = section is not None or headings_only
    if section_focused and not include_description:
        _raise_tool_error("section and headings_only require include_description=true.")
    if section_focused and description_limit == 0:
        _raise_tool_error("section and headings_only require description_limit greater than 0.")
    if find is None:
        if find_context:
            _raise_tool_error("find_context requires find.")
        return section_focused
    if section_focused:
        _raise_tool_error("find cannot be combined with section or headings_only.")
    if not include_description:
        _raise_tool_error("find requires include_description=true.")
    if description_limit == 0:
        _raise_tool_error("find requires description_limit greater than 0.")
    _validate_find_terms(find)
    return True


def _validate_find_terms(find: list[str]) -> None:
    """Reject empty, excessive, multiline, or oversized literal terms."""
    if not find or len(find) > FIND_TERM_CAP:
        _raise_tool_error("find requires 1 to 20 terms.")
    if any(not term or len(term) > FIND_TERM_LENGTH or "\n" in term or "\r" in term for term in find):
        _raise_tool_error("find terms must be 1 to 200 characters and must not contain CR or LF.")


def find_description_lines(description: str, terms: list[str], context: int, limit: int) -> tuple[str, list[str], bool]:
    """Find literal terms in raw lines and return bounded TSV, missing terms, and truncation status."""
    lines = description.split("\n")
    offsets: list[int] = []
    hits: dict[int, list[int]] = {}
    found: set[int] = set()
    offset = 0
    for index, line in enumerate(lines):
        offsets.append(offset)
        offset += len(line) + 1
        matches = [number for number, term in enumerate(terms, 1) if term in line]
        if matches:
            hits[index] = matches
            found.update(matches)

    missing = [term for number, term in enumerate(terms, 1) if number not in found]
    rendered: dict[int, tuple[str, bool]] = {}
    remaining = limit
    truncated = len(hits) > FIND_HIT_CAP
    for index in list(hits)[:FIND_HIT_CAP]:
        if remaining == 0:
            truncated = True
            continue
        text = lines[index][:remaining]
        cut = len(text) < len(lines[index])
        rendered[index] = (text, cut)
        remaining -= len(text)
        truncated |= cut

    # 🧱 Context only surrounds rendered hits. Empty lines cost no budget, so context around
    # hits cut by the cap would otherwise add unbounded blank rows.
    context_lines = {
        index
        for hit in list(rendered)
        for index in range(max(0, hit - context), min(len(lines), hit + context + 1))
        if index not in hits
    }
    for index in sorted(context_lines):
        text = lines[index]
        if len(text) > remaining:
            truncated = True
            continue
        rendered[index] = (text, False)
        remaining -= len(text)

    rows = ["line\toffset\tterms\tcut\ttext"]
    for index, (text, cut) in sorted(rendered.items()):
        term_indexes = ",".join(str(number) for number in hits.get(index, []))
        rows.append(
            f"{index + 1}\t{offsets[index]}\t{term_indexes}\t{'true' if cut else ''}\t"
            f"{json.dumps(text, ensure_ascii=False)}"
        )
    return "\n".join(rows), missing, truncated


def section_selectors(section: str | list[str] | None) -> list[str] | None:
    """Validate selectors and return them as a list when focused selection was requested."""
    if section is None:
        return None
    if isinstance(section, str):
        selectors = [section]
    elif isinstance(section, list):
        selectors = section
        if not selectors:
            _raise_tool_error("section lists must not be empty.")
    else:
        _raise_tool_error("section must be a string, a list of strings, or null.")
    if any(not isinstance(selector, str) or not selector.strip() for selector in selectors):
        _raise_tool_error("section selectors must be non-blank strings.")
    normalized_selectors = [selector.strip().casefold() for selector in selectors]
    if len(set(normalized_selectors)) != len(normalized_selectors):
        _raise_tool_error("section selectors must not contain duplicates.")
    return selectors


def markdown_sections(description: str) -> list[tuple[str, str]]:
    """Extract ATX heading sections, ignoring heading-like lines inside fenced code."""
    headings: list[dict[str, Any]] = []
    open_sections: list[int] = []
    fence_character = ""
    fence_length = 0
    position = 0
    for line in description.splitlines(keepends=True):
        line_text = line.rstrip("\r\n")
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line_text)
        if fence_character:
            if (
                fence_match
                and fence_match.group(1)[0] == fence_character
                and len(fence_match.group(1)) >= fence_length
                and not fence_match.group(2).strip()
            ):
                fence_character = ""
                fence_length = 0
            position += len(line)
            continue
        if fence_match:
            fence_character = fence_match.group(1)[0]
            fence_length = len(fence_match.group(1))
            position += len(line)
            continue

        heading_match = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*?)\s*$", line_text)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = re.sub(r"[ \t]+#+[ \t]*$", "", heading_match.group(2)).strip()
            while open_sections and headings[open_sections[-1]]["level"] >= level:
                headings[open_sections.pop()]["end"] = position
            headings.append(
                {
                    "level": level,
                    "heading": heading_text,
                    "body_start": position + len(line),
                    "end": len(description),
                }
            )
            open_sections.append(len(headings) - 1)
        position += len(line)

    return [
        (heading["heading"], description[heading["body_start"] : heading["end"]].strip("\r\n")) for heading in headings
    ]


def _leading_heading_id(value: str) -> str:
    """Return a complete leading ID token when one is present in a heading."""
    value = value.strip()
    match = re.match(r"^([A-Za-z]+(?:[-_ ]?\d+)?)(?=$|[\s:./])", value)
    return match.group(1) if match else ""


def _normalize_heading_id(value: str) -> str:
    """Normalize separators and case in a leading heading ID."""
    return re.sub(r"[-_ ]", "", value).casefold()


def select_section(
    headings: list[tuple[str, str]],
    selector: str,
) -> tuple[str, str]:
    """Resolve an exact heading or a unique complete leading ID, with clear errors."""
    exact_matches = [heading for heading in headings if heading[0].casefold() == selector.strip().casefold()]
    if exact_matches:
        matches = exact_matches
    else:
        selector_id = selector.strip()
        if not re.fullmatch(r"[A-Za-z]+(?:[-_ ]?\d+)?", selector_id):
            matches = []
        else:
            normalized_selector = _normalize_heading_id(selector_id)
            matches = [
                heading
                for heading in headings
                if _normalize_heading_id(_leading_heading_id(heading[0])) == normalized_selector
            ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        candidates = ", ".join(repr(heading[0]) for heading in matches)
        _raise_tool_error(f"Section selector {selector!r} is ambiguous. Candidates: {candidates}.")
    candidates = ", ".join(repr(heading[0]) for heading in headings) or "none"
    _raise_tool_error(f"Section {selector!r} was not found. Available headings: {candidates}.")


def focused_description(
    description: str,
    section: str | list[str] | None,
    headings_only: bool,
    limit: int,
) -> tuple[str | None, bool, list[str] | None, list[DescriptionSection] | None]:
    """Return a headings list or the selected sections of a description, capped at `limit` characters in total.

    The result is `(description, description_truncated, headings, sections)`. A string `section` fills
    `description`, a list fills `sections`, and `headings_only` fills `headings`.
    """
    parsed_sections = markdown_sections(description)
    if headings_only or section is None:
        return None, False, [heading for heading, _ in parsed_sections], None
    if isinstance(section, str):
        _, content = select_section(parsed_sections, section)
        return content[:limit], len(content) > limit, None, None
    resolved_sections = [select_section(parsed_sections, selector) for selector in section]
    resolved_headings = [heading.casefold() for heading, _ in resolved_sections]
    if len(set(resolved_headings)) != len(resolved_headings):
        _raise_tool_error("Section selectors resolve to the same heading more than once.")
    selected_sections: list[DescriptionSection] = []
    remaining = limit
    for heading, content in resolved_sections:
        bounded_content = content[:remaining]
        selected_sections.append(
            DescriptionSection(heading=heading, content=bounded_content, truncated=len(content) > remaining)
        )
        remaining -= len(bounded_content)
    return None, False, None, selected_sections
