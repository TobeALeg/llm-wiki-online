"""Structure-aware chunking for the LLM Wiki ingest pipeline.

A chunk is one contiguous run of a source document, small enough to hand to the
model, tagged with the ATX heading path that was in effect where it started.
Chunks come out in document order and never overlap, so a long file is read from
its first byte to its last instead of being excerpted.

Offsets are counted in Unicode code points over the saved normalized text, and
``[start, end)`` is half-open.  Python strings index by code point, so
``text[start:end]`` is the whole translation; UTF-8 or UTF-16 lengths are never
mixed in.

``start``/``end`` always identify the original text a chunk covers, and
``text`` is what the model sees.  For a heading, a paragraph, or a character
run the two agree, ``verbatim`` is True, and ``render_recipe`` is
``verbatim``.  For an oversized code fence or table they do not: each piece
repeats the opening fence or the header and separator rows so it can stand
alone, ``start``/``end`` cover only the content lines that piece carries, and
the recipe names the reconstruction.  ``evidence_spans`` lists what is really
in the source; ``context_spans`` lists the lines the renderer re-emitted.  That
split is what keeps a fabricated fence from being cited as source text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

PARSER_NAME = "structural"
PARSER_VERSION = "1"
TARGET_CHUNK_CHARS = 12_000
MAX_CHUNK_CHARS = 16_000

RECIPE_VERBATIM = "verbatim"
RECIPE_TABLE_WITH_HEADER = "table_with_header"
RECIPE_CODE_WITH_FENCE = "code_with_fence"
RECIPE_CHARACTER_WINDOW = "character_window"

_DOMAIN = "llm-wiki/chunking"
_FIELD_SEP = "\x00"
_FIELD_PART_SEP = "\x1f"

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]|$)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_PIPE_START_RE = re.compile(r"^ {0,3}\|")

_SENTENCE_ENDINGS = "。！？!?."

_FOOTNOTE_PREFIXES = ("[^", "*", "†", "‡", "注", "Note:", "note:")

OFFSET_UNIT = "unicode_code_point"
"""Coordinates are Unicode code points over the saved normalized text.

Repeated verbatim from `evidence.py` rather than imported, because this module
ships standalone inside the skill package and imports no sibling. A test asserts
the two constants agree.
"""


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    index: int
    start: int
    end: int
    heading_path: tuple[str, ...]
    text: str
    verbatim: bool
    evidence_spans: tuple[tuple[int, int], ...] = ()
    context_spans: tuple[tuple[int, int], ...] = ()
    render_recipe: str = RECIPE_VERBATIM
    render_notes: tuple[str, ...] = ()

    def evidence(self) -> tuple[tuple[int, int], ...]:
        """The ranges a reader can verify against the source.

        Falls back to the covered range so a chunk built before this metadata
        existed still names something real rather than nothing.
        """

        return self.evidence_spans or ((self.start, self.end),)


@dataclass(frozen=True)
class _Piece:
    """One chunk's worth of text plus how it was addressed and reconstructed."""

    start: int
    end: int
    text: str
    evidence_spans: tuple[tuple[int, int], ...]
    context_spans: tuple[tuple[int, int], ...] = ()
    recipe: str = RECIPE_VERBATIM
    notes: tuple[str, ...] = ()


@dataclass
class _Block:
    kind: str
    start: int
    end: int
    path: tuple[str, ...] = ()
    level: int = 0
    heading_text: str = ""
    open_line: tuple[int, int] | None = None
    close_line: tuple[int, int] | None = None
    header_line: tuple[int, int] | None = None
    separator_line: tuple[int, int] | None = None
    body_lines: tuple[tuple[int, int], ...] = ()
    footnote_lines: tuple[int, ...] = field(default=())


def chunk_text(
    text: str,
    *,
    target_chars: int = TARGET_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[Chunk]:
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    chunks: list[Chunk] = []
    buffer: list[_Block] = []
    stack: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        first, last = buffer[0], buffer[-1]
        if last.end > first.start:
            _emit(chunks, text, first.start, last.end, first.path)
        buffer.clear()

    for block in _scan(text):
        if block.kind == "heading":
            flush()
            stack = stack[: block.level - 1] + [block.heading_text]
            block.path = tuple(stack)
            if block.end - block.start > max_chars:
                _emit_oversized(chunks, text, block, max_chars)
                continue
            buffer.append(block)
            if block.end - block.start >= target_chars:
                flush()
            continue

        block.path = tuple(stack)
        if buffer and block.end - buffer[0].start > max_chars:
            flush()
        if block.end - block.start > max_chars:
            flush()
            _emit_oversized(chunks, text, block, max_chars)
            continue
        buffer.append(block)
        if buffer[-1].end - buffer[0].start >= target_chars:
            flush()

    flush()
    return chunks


def parse_id(
    revision_id: str,
    chunks: list[Chunk],
    *,
    target_chars: int = TARGET_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
) -> str:
    digest = hashlib.sha256()
    _feed(digest, _DOMAIN, PARSER_VERSION, target_chars, max_chars, revision_id)
    for chunk in chunks:
        _feed(
            digest,
            chunk.chunk_id,
            chunk.index,
            chunk.start,
            chunk.end,
            _FIELD_PART_SEP.join(chunk.heading_path),
            len(chunk.text),
            chunk.text,
        )
    return "sha256:" + digest.hexdigest()


def _feed(digest: "hashlib._Hash", *fields: object) -> None:
    for field in fields:
        digest.update(str(field).encode("utf-8"))
        digest.update(_FIELD_SEP.encode("utf-8"))


def _emit(
    chunks: list[Chunk],
    text: str,
    start: int,
    end: int,
    path: tuple[str, ...],
    body: str | None = None,
) -> None:
    if body is None:
        body = text[start:end]
    index = len(chunks) + 1
    chunks.append(
        Chunk(
            chunk_id=f"chunk-{index:03d}",
            index=index,
            start=start,
            end=end,
            heading_path=path,
            text=body,
            verbatim=body == text[start:end],
            evidence_spans=((start, end),),
            context_spans=(),
            render_recipe=RECIPE_VERBATIM,
        )
    )


def _emit_piece(chunks: list[Chunk], text: str, path: tuple[str, ...], piece: _Piece) -> None:
    index = len(chunks) + 1
    chunks.append(
        Chunk(
            chunk_id=f"chunk-{index:03d}",
            index=index,
            start=piece.start,
            end=piece.end,
            heading_path=path,
            text=piece.text,
            verbatim=piece.text == text[piece.start : piece.end],
            evidence_spans=piece.evidence_spans,
            context_spans=piece.context_spans,
            render_recipe=piece.recipe,
            render_notes=piece.notes,
        )
    )


def _emit_oversized(
    chunks: list[Chunk], text: str, block: _Block, max_chars: int
) -> None:
    for piece in _oversized_pieces(text, block, max_chars):
        _emit_piece(chunks, text, block.path, piece)


def _oversized_pieces(text: str, block: _Block, max_chars: int) -> list[_Piece]:
    if block.kind == "code":
        return _code_pieces(text, block, max_chars)
    if block.kind == "table":
        return _table_pieces(text, block, max_chars)
    return _windowed_pieces(text, _paragraph_pieces(text, block.start, block.end, max_chars))


def _windowed_pieces(text: str, spans: list[tuple[int, int]]) -> list[_Piece]:
    return [
        _Piece(
            start=start,
            end=end,
            text=text[start:end],
            evidence_spans=((start, end),),
            recipe=RECIPE_CHARACTER_WINDOW,
            notes=("long_paragraph_windowed",),
        )
        for start, end in spans
    ]


def _code_pieces(text: str, block: _Block, max_chars: int) -> list[_Piece]:
    assert block.open_line is not None
    opening = _line_text(text, block.open_line)
    closing_line = block.close_line
    closing = _line_text(text, closing_line) if closing_line else opening
    # An unterminated fence has no closing line in the source. The renderer still
    # emits one so the piece is readable, and the note records that it is ours.
    closing_is_source = closing_line is not None
    lines = [_line_text(text, span) for span in block.body_lines]
    budget = max_chars - (len(opening) + len(closing) + 2)
    if budget < 1 or any(len(line) > budget for line in lines):
        return [
            _Piece(
                start=start,
                end=end,
                text=text[start:end],
                evidence_spans=((start, end),),
                recipe=RECIPE_CHARACTER_WINDOW,
                notes=("code_windowed_verbatim",),
            )
            for start, end in _character_pieces(text, block.start, block.end, max_chars)
        ]

    pieces: list[_Piece] = []
    group: list[int] = []
    used = 0
    for position, line in enumerate(lines):
        cost = len(line) + (1 if group else 0)
        if group and used + cost > budget:
            pieces.append(_code_piece(text, block, group, opening, closing, closing_is_source))
            group, used = [], 0
            cost = len(line)
        group.append(position)
        used += cost
    if group:
        pieces.append(_code_piece(text, block, group, opening, closing, closing_is_source))
    return pieces


def _code_piece(
    text: str,
    block: _Block,
    group: list[int],
    opening: str,
    closing: str,
    closing_is_source: bool,
) -> _Piece:
    spans = tuple(block.body_lines[position] for position in group)
    start, end = _covered_range(text, list(spans))
    body = "\n".join([opening, *[_line_text(text, span) for span in spans], closing])
    context = (block.open_line,) if block.open_line else ()
    if closing_is_source and block.close_line:
        context = (*context, block.close_line)
    notes = ["fence_repeated"] if closing_is_source else ["fence_closed_synthesized"]
    return _Piece(
        start=start,
        end=end,
        text=body,
        evidence_spans=spans,
        context_spans=context,
        recipe=RECIPE_CODE_WITH_FENCE,
        notes=tuple(notes),
    )


def _table_pieces(text: str, block: _Block, max_chars: int) -> list[_Piece]:
    assert block.header_line is not None and block.separator_line is not None
    header = _line_text(text, block.header_line)
    separator = _line_text(text, block.separator_line)
    rows = [_line_text(text, span) for span in block.body_lines]
    budget = max_chars - (len(header) + len(separator) + 2)
    if budget < 1 or not rows or any(len(row) > budget for row in rows):
        return [
            _Piece(
                start=start,
                end=end,
                text=text[start:end],
                evidence_spans=((start, end),),
                recipe=RECIPE_CHARACTER_WINDOW,
                notes=("table_windowed_verbatim",),
            )
            for start, end in _character_pieces(text, block.start, block.end, max_chars)
        ]

    pieces: list[_Piece] = []
    group: list[int] = []
    used = 0
    for position, row in enumerate(rows):
        cost = len(row) + (1 if group else 0)
        if group and used + cost > budget:
            pieces.append(_table_piece(text, block, group, header, separator))
            group, used = [], 0
            cost = len(row)
        group.append(position)
        used += cost
    if group:
        pieces.append(_table_piece(text, block, group, header, separator))
    return pieces


def _table_piece(
    text: str, block: _Block, group: list[int], header: str, separator: str
) -> _Piece:
    spans = tuple(block.body_lines[position] for position in group)
    start, end = _covered_range(text, list(spans))
    body = "\n".join([header, separator, *[_line_text(text, span) for span in spans]])
    return _Piece(
        start=start,
        end=end,
        text=body,
        evidence_spans=spans,
        context_spans=(block.header_line, block.separator_line),  # type: ignore[arg-type]
        recipe=RECIPE_TABLE_WITH_HEADER,
        notes=("header_repeated", "separator_repeated"),
    )


def _paragraph_pieces(
    text: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    open_span: list[int] | None = None
    for span_start, span_end in _sentence_spans(text, start, end):
        if span_end - span_start > max_chars:
            if open_span is not None:
                pieces.append((open_span[0], open_span[1]))
                open_span = None
            pieces.extend(_character_pieces(text, span_start, span_end, max_chars))
        elif open_span is None:
            open_span = [span_start, span_end]
        elif span_end - open_span[0] <= max_chars:
            open_span[1] = span_end
        else:
            pieces.append((open_span[0], open_span[1]))
            open_span = [span_start, span_end]
    if open_span is not None:
        pieces.append((open_span[0], open_span[1]))
    return pieces


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    sentence_start = start
    position = start
    while position < end:
        char = text[position]
        if char in _SENTENCE_ENDINGS or char == "\n":
            boundary = position + 1
            if boundary >= end or text[boundary].isspace():
                spans.append((sentence_start, boundary))
                sentence_start = boundary
        position += 1
    if sentence_start < end:
        spans.append((sentence_start, end))
    return spans


def _character_pieces(
    text: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    position = start
    while position < end:
        limit = min(position + max_chars, end)
        if limit < end:
            window = text[position:limit]
            newline = window.rfind("\n")
            space = max(window.rfind(" "), window.rfind("\t"))
            # Cut after the boundary so the pieces still tile the source exactly.
            if newline >= 0:
                limit = position + newline + 1
            elif space >= 0:
                limit = position + space + 1
        pieces.append((position, limit))
        position = limit
    return pieces


def _scan(text: str) -> list[_Block]:
    lines = _line_spans(text)
    blocks: list[_Block] = []
    total = len(lines)
    position = 0
    while position < total:
        line = _line_text(text, lines[position])
        if not line.strip():
            position += 1
            continue

        level = _heading_level(line)
        if level:
            span = lines[position]
            blocks.append(
                _Block(
                    kind="heading",
                    start=span[0],
                    end=span[1],
                    level=level,
                    heading_text=_heading_text(line, level),
                )
            )
            position += 1
            continue

        fence = _fence_open(line)
        if fence is not None:
            position = _scan_code(text, lines, position, fence, blocks)
            continue

        if _is_table_start(text, lines, position):
            position = _scan_table(text, lines, position, blocks)
            continue

        position = _scan_paragraph(text, lines, position, blocks)
    return blocks


def _scan_code(
    text: str,
    lines: list[tuple[int, int]],
    position: int,
    fence: tuple[str, str],
    blocks: list[_Block],
) -> int:
    fence_text, _info = fence
    opening = lines[position]
    content: list[tuple[int, int]] = []
    cursor = position + 1
    closing: tuple[int, int] | None = None
    while cursor < len(lines):
        if _is_fence_close(_line_text(text, lines[cursor]), fence_text):
            closing = lines[cursor]
            break
        content.append(lines[cursor])
        cursor += 1

    if closing is not None:
        block_end = closing[1]
        following = cursor + 1
    else:
        block_end = lines[-1][1] if lines else opening[1]
        following = len(lines)

    blocks.append(
        _Block(
            kind="code",
            start=opening[0],
            end=block_end,
            open_line=opening,
            close_line=closing,
            body_lines=tuple(content),
        )
    )
    return following


def _scan_table(
    text: str, lines: list[tuple[int, int]], position: int, blocks: list[_Block]
) -> int:
    header = lines[position]
    separator = lines[position + 1]
    body: list[tuple[int, int]] = []
    cursor = position + 2
    while cursor < len(lines) and _PIPE_START_RE.match(_line_text(text, lines[cursor])):
        body.append(lines[cursor])
        cursor += 1

    # A table's units live in the marked lines under it as often as in the header.
    footnotes: list[int] = []
    probe = cursor
    while probe < len(lines):
        stripped = _line_text(text, lines[probe]).strip()
        if not stripped or not stripped.startswith(_FOOTNOTE_PREFIXES):
            break
        footnotes.append(probe)
        probe += 1

    block_end = body[-1][1] if body else separator[1]
    blocks.append(
        _Block(
            kind="table",
            start=header[0],
            end=block_end,
            header_line=header,
            separator_line=separator,
            body_lines=tuple(body),
            footnote_lines=tuple(footnotes),
        )
    )
    return cursor


def _scan_paragraph(
    text: str, lines: list[tuple[int, int]], position: int, blocks: list[_Block]
) -> int:
    body = [lines[position]]
    cursor = position + 1
    while cursor < len(lines):
        line = _line_text(text, lines[cursor])
        if (
            not line.strip()
            or _heading_level(line)
            or _fence_open(line) is not None
            or _is_table_start(text, lines, cursor)
        ):
            break
        body.append(lines[cursor])
        cursor += 1

    blocks.append(
        _Block(
            kind="paragraph",
            start=body[0][0],
            end=body[-1][1],
            body_lines=tuple(body),
        )
    )
    return cursor


def _heading_level(line: str) -> int:
    match = _HEADING_RE.match(line)
    return len(match.group(1)) if match else 0


def _heading_text(line: str, level: int) -> str:
    return line.strip()[level:].strip()


def _fence_open(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    for character in ("`", "~"):
        if not stripped.startswith(character * 3):
            continue
        run = 0
        while run < len(stripped) and stripped[run] == character:
            run += 1
        return character * run, stripped[run:].strip()
    return None


def _is_fence_close(line: str, fence: str) -> bool:
    stripped = line.strip()
    return (
        len(stripped) >= len(fence)
        and stripped == stripped[0] * len(stripped)
        and stripped[0] == fence[0]
    )


def _is_table_start(text: str, lines: list[tuple[int, int]], index: int) -> bool:
    if not _PIPE_START_RE.match(_line_text(text, lines[index])):
        return False
    if index + 1 >= len(lines):
        return False
    return bool(_TABLE_SEP_RE.match(_line_text(text, lines[index + 1])))


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    position = 0
    total = len(text)
    while position < total:
        newline = text.find("\n", position)
        if newline == -1:
            spans.append((position, total))
            break
        spans.append((position, newline))
        position = newline + 1
    return spans


def _line_text(text: str, span: tuple[int, int]) -> str:
    return text[span[0] : span[1]]


def _covered_range(
    text: str, spans: list[tuple[int, int]]
) -> tuple[int, int]:
    start, end = spans[0][0], spans[-1][1]
    if end <= start:
        end = min(len(text), start + 1)
    return start, end


def artifact_structure(text: str) -> dict:
    """The block index saved beside a parse artifact.

    Reading a table's header and units, or a code block's real definition, is
    what a citation needs and what re-running a parser would otherwise be
    required for. Saving this index with the artifact means an old citation
    still resolves on a machine where that parser is gone.
    """

    lines = _line_spans(text)
    entries: list[dict] = []
    stack: list[str] = []
    for block in _scan(text):
        if block.kind == "heading":
            stack = stack[: block.level - 1] + [block.heading_text]
            block.path = tuple(stack)
        else:
            block.path = tuple(stack)

        entry: dict = {
            "kind": block.kind,
            "start": block.start,
            "end": block.end,
            "heading_path": list(block.path),
        }
        if block.kind == "heading":
            entry["level"] = block.level
            entry["heading_text"] = block.heading_text
        elif block.kind == "code":
            context = [block.open_line]
            if block.close_line:
                context.append(block.close_line)
            entry["context_spans"] = [[span[0], span[1]] for span in context if span]
            entry["fence_unterminated"] = block.close_line is None
        elif block.kind == "table":
            entry["context_spans"] = [
                [block.header_line[0], block.header_line[1]],
                [block.separator_line[0], block.separator_line[1]],
            ]
            entry["footnote_lines"] = list(block.footnote_lines)
        entries.append(entry)

    return {
        "offset_unit": OFFSET_UNIT,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "line_count": len(lines),
        "lines": [[start, end] for start, end in lines],
        "blocks": entries,
    }
