"""Conservative Markdown table-cell selection and replacement for descriptions 🧾."""

import re
from itertools import pairwise
from typing import NoReturn

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, field_validator

FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
TABLE_SEPARATOR = re.compile(r":?-{3,}:?")
MIN_TABLE_ROWS = 2
MIN_TABLE_COLUMNS = 2


class DescriptionTableCell(BaseModel):
    """Select one cell by its table's first-column key and column heading."""

    model_config = ConfigDict(extra="forbid")

    row_key: str = Field(description="Exact first-column text identifying one row.")
    column: str = Field(description="Exact table header identifying the cell to replace.")
    new_text: str = Field(description="Replacement text. Empty text clears the cell.")
    section: str | None = Field(
        default=None,
        description="Optional exact ATX heading text. Selects that section through the next same-or-higher heading.",
    )

    @field_validator("row_key", "column", "section")
    @classmethod
    def _trim_selector(cls, value: str | None) -> str | None:
        """Trim selectors and reject required or supplied selectors that are blank."""
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Selectors must not be blank.")  # noqa: TRY003 - explain field validation errors.
        return trimmed

    @field_validator("new_text")
    @classmethod
    def _validate_replacement(cls, value: str) -> str:
        """Reject cell text that can change the supported table structure."""
        if any(character in value for character in "|\r\n\\`"):
            raise ValueError("new_text contains an unsupported table character.")  # noqa: TRY003 - explain invalid input.
        return value


def _raise_table_error(message: str) -> NoReturn:
    """Raise one caller-facing error for an unsafe or unmatched table selection."""
    raise ToolError(message)


def _source_lines(description: str) -> list[tuple[str, int]]:
    """Return each source line without its newline and with its original character offset."""
    lines = []
    start = 0
    while start < len(description):
        newline = description.find("\n", start)
        end = len(description) if newline == -1 else newline + 1
        content_end = end - 1 if newline != -1 else end
        if content_end > start and description[content_end - 1] == "\r":
            content_end -= 1
        lines.append((description[start:content_end], start))
        start = end
    return lines


def _fence_start(line: str) -> tuple[str, int] | None:
    """Return the fence character and length for a valid opening fence line."""
    match = FENCE_OPEN.fullmatch(line)
    if match is None:
        return None
    fence = match.group(1)
    info = match.group(2)
    if fence.startswith("`") and "`" in info:
        return None
    return fence[0], len(fence)


def _is_fence_end(line: str, fence_char: str, fence_length: int) -> bool:
    """Return whether a line closes the active fence with a long enough marker."""
    match = FENCE_CLOSE.fullmatch(line)
    return match is not None and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_length


def _outside_fence_mask(lines: list[tuple[str, int]]) -> list[bool]:
    """Mark lines outside fenced code, including unclosed fences through end of input."""
    outside = []
    fence_char = None
    fence_length = 0
    for line, _ in lines:
        if fence_char is not None:
            outside.append(False)
            if _is_fence_end(line, fence_char, fence_length):
                fence_char = None
            continue
        opening = _fence_start(line)
        outside.append(opening is None)
        if opening is not None:
            fence_char, fence_length = opening
    return outside


def _heading(line: str) -> tuple[int, str] | None:
    """Parse an ATX heading into its level and exact visible text, or return none."""
    match = ATX_HEADING.fullmatch(line)
    if match is None:
        return None
    text = (match.group(2) or "").strip()
    text = re.sub(r"[ \t]+#+[ \t]*$", "", text).rstrip()
    return len(match.group(1)), text


def _section_range(lines: list[tuple[str, int]], outside_fence: list[bool], section: str | None) -> tuple[int, int]:
    """Return the line range selected by an optional unique ATX heading."""
    if section is None:
        return 0, len(lines)

    headings = [
        (index, parsed[0], parsed[1])
        for index, (line, _) in enumerate(lines)
        if outside_fence[index]
        if (parsed := _heading(line)) is not None
    ]
    matches = [(index, level) for index, level, text in headings if text == section]
    if not matches:
        _raise_table_error(f"Section '{section}' was not found.")
    if len(matches) > 1:
        _raise_table_error(f"Section '{section}' appears more than once.")

    start, level = matches[0]
    end = len(lines)
    for next_index, next_level, _ in headings:
        if next_index > start and next_level <= level:
            end = next_index
            break
    return start + 1, end


def _pipe_blocks(
    lines: list[tuple[str, int]], outside_fence: list[bool], start: int, end: int
) -> list[list[tuple[str, int]]]:
    """Collect pipe runs that have explicit standalone Markdown boundaries."""
    blocks = []

    def is_boundary(index: int) -> bool:
        """Check a neighboring source line for one of the supported table boundaries."""
        if index < 0 or index >= len(lines) or not outside_fence[index]:
            return True
        line = lines[index][0]
        return not line.strip() or _heading(line) is not None

    index = start
    while index < end:
        line = lines[index]
        if not outside_fence[index] or "|" not in line[0] or _heading(line[0]) is not None:
            index += 1
            continue

        block_start = index
        while index < end and outside_fence[index] and "|" in lines[index][0] and _heading(lines[index][0]) is None:
            index += 1
        if not is_boundary(block_start - 1) or not is_boundary(index):
            _raise_table_error("Pipe rows must be separated from surrounding text by a blank line or heading.")
        blocks.append(lines[block_start:index])
    return blocks


def _parse_row(line: str, offset: int) -> tuple[list[str], list[tuple[int, int]]]:
    """Parse one standalone pipe-delimited row and return its cells and source spans."""
    trimmed = line.rstrip()
    if not trimmed.startswith("|") or not trimmed.endswith("|"):
        _raise_table_error("Malformed table row: supported rows must start and end with a pipe.")
    if "\\" in trimmed or "`" in trimmed:
        _raise_table_error("Candidate table rows cannot contain backslashes or backticks.")

    delimiters = [index for index, character in enumerate(trimmed) if character == "|"]
    cells = []
    spans = []
    for left, right in pairwise(delimiters):
        cells.append(trimmed[left + 1 : right])
        spans.append((offset + left + 1, offset + right))
    return cells, spans


def _parse_table(block: list[tuple[str, int]]) -> tuple[list[str], list[list[str]], list[list[tuple[int, int]]]]:
    """Validate a candidate table and return stripped headers, rows, and cell spans."""
    if len(block) < MIN_TABLE_ROWS:
        _raise_table_error("Malformed table: a header and separator row are required.")

    parsed = [_parse_row(line, offset) for line, offset in block]
    headers = [cell.strip() for cell in parsed[0][0]]
    width = len(headers)
    if width < MIN_TABLE_COLUMNS:
        _raise_table_error("Malformed table: at least two columns are required.")
    if any(not header for header in headers):
        _raise_table_error("Malformed table: headers must not be empty.")
    if len(set(headers)) != width:
        _raise_table_error("Malformed table: headers must be unique.")
    if len(parsed[1][0]) != width:
        _raise_table_error("Malformed table: separator width does not match the header.")
    if any(TABLE_SEPARATOR.fullmatch(cell.strip()) is None for cell in parsed[1][0]):
        _raise_table_error("Malformed table: separator cells need at least three dashes, with optional colons.")

    rows = []
    spans = []
    for cells, cell_spans in parsed[2:]:
        if len(cells) != width:
            _raise_table_error("Malformed table: a body row has a different number of columns.")
        rows.append(cells)
        spans.append(cell_spans)
    return headers, rows, spans


def _cell_padding(cell: str) -> tuple[str, str]:
    """Separate cell padding from its content without duplicating all-space cells."""
    left = len(cell) - len(cell.lstrip())
    right = len(cell.rstrip())
    if left <= right:
        return cell[:left], cell[right:]
    midpoint = len(cell) // 2
    return cell[:midpoint], cell[midpoint:]


def replace_table_cell(description: str, selection: DescriptionTableCell) -> str:
    """Return a copy with the unique selected cell changed and every other character preserved."""
    lines = _source_lines(description)
    outside_fence = _outside_fence_mask(lines)
    if any(outside and "<" in line for (line, _), outside in zip(lines, outside_fence, strict=True)):
        _raise_table_error(
            "Table-cell mode does not support HTML-like content outside fenced code. "
            "Use legacy or batch exact-text replacement instead."
        )
    scope_start, scope_end = _section_range(lines, outside_fence, selection.section)
    matches = []

    for block in _pipe_blocks(lines, outside_fence, scope_start, scope_end):
        headers, rows, cell_spans = _parse_table(block)
        if selection.column not in headers:
            continue
        column_index = headers.index(selection.column)
        for row, spans in zip(rows, cell_spans, strict=True):
            if row[0].strip() == selection.row_key:
                matches.append(spans[column_index])

    if not matches:
        _raise_table_error("No matching table cell was found for the selected row and column.")
    if len(matches) > 1:
        _raise_table_error("The selected table cell matches more than once and is ambiguous.")

    start, end = matches[0]
    original = description[start:end]
    prefix, suffix = _cell_padding(original)
    replacement = prefix + selection.new_text + suffix
    return description[:start] + replacement + description[end:]
