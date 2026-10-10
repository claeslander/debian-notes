"""Decode Apple Notes' internal formats into note HTML.

Apple stores a note's body as a gzipped protobuf ("NoteStoreProto"): the plain text plus attribute
runs carrying paragraph styles (title, lists, checklists…), inline styles and attachment markers.
Tables are separate "mergeable data" protobufs (a CRDT). The field numbers follow the community
documentation of the format (apple_cloud_notes_parser); no protobuf library is needed.
"""

import gzip
import html
import re
import struct
import zlib

OBJECT_REPLACEMENT = "￼"

STYLE_TITLE, STYLE_HEADING, STYLE_SUBHEADING, STYLE_MONO = 0, 1, 2, 4
STYLE_BULLET, STYLE_DASHED, STYLE_NUMBERED, STYLE_CHECKLIST = 100, 101, 102, 103
LIST_STYLES = {STYLE_BULLET, STYLE_DASHED, STYLE_NUMBERED, STYLE_CHECKLIST}
HASHTAG_RE = re.compile(r"(?<![\w#&/])#(\w[\w\-]*)", re.UNICODE)


# --------------------------------------------------------------- protobuf wire format


class Msg:
    """A parsed protobuf message: field number -> list of raw values (int, float or bytes)."""

    __slots__ = ("fields",)

    def __init__(self, data=b""):
        self.fields = {}
        if data:
            self._parse(data)

    def _parse(self, buf):
        i, n = 0, len(buf)
        while i < n:
            key, i = _varint(buf, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, i = _varint(buf, i)
            elif wire == 1:
                value = struct.unpack_from("<d", buf, i)[0]
                i += 8
            elif wire == 2:
                length, i = _varint(buf, i)
                value = bytes(buf[i:i + length])
                if len(value) != length:
                    raise ValueError("truncated protobuf")
                i += length
            elif wire == 5:
                value = struct.unpack_from("<f", buf, i)[0]
                i += 4
            else:
                raise ValueError(f"unsupported wire type {wire}")
            self.fields.setdefault(field, []).append(value)

    def has(self, n):
        return n in self.fields

    def int(self, n, default=0):
        v = self.fields.get(n)
        return v[0] if v and isinstance(v[0], int) else default

    def float(self, n, default=0.0):
        v = self.fields.get(n)
        return v[0] if v and isinstance(v[0], float) else default

    def bytes(self, n, default=b""):
        v = self.fields.get(n)
        return v[0] if v and isinstance(v[0], bytes) else default

    def str(self, n, default=""):
        b = self.bytes(n, None)
        return b.decode("utf-8", "replace") if b is not None else default

    def msg(self, n):
        b = self.bytes(n, None)
        return Msg(b) if b is not None else None

    def msgs(self, n):
        return [Msg(v) for v in self.fields.get(n, []) if isinstance(v, bytes)]

    def strs(self, n):
        return [v.decode("utf-8", "replace") for v in self.fields.get(n, []) if isinstance(v, bytes)]

    def raw_list(self, n):
        return list(self.fields.get(n, []))


def _varint(buf, i):
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def decompress(data):
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:1] == b"\x78":
        return zlib.decompress(data)
    return data


# ------------------------------------------------------------------- note documents


def parse_note_store(data):
    """NoteStoreProto(2: Document(3: Note)) -> the Note message."""
    root = Msg(decompress(data))
    doc = root.msg(2)
    note = doc.msg(3) if doc else None
    if note is None:
        raise ValueError("not a note document")
    return note


def note_runs(note):
    """Split a Note message into (text, AttributeRun) pieces. Run lengths count UTF-16 units."""
    text = note.str(2)
    units = text.encode("utf-16-le")
    pos = 0
    out = []
    for run in note.msgs(5):
        length = run.int(1)
        piece = units[pos * 2:(pos + length) * 2].decode("utf-16-le", "replace")
        pos += length
        out.append((piece, run))
    if pos * 2 < len(units):
        out.append((units[pos * 2:].decode("utf-16-le", "replace"), Msg()))
    return out


def plain_text(note):
    return note.str(2).replace(OBJECT_REPLACEMENT, "")


def _paragraphs(note):
    """Group runs into paragraphs: [{'pieces': [(text, run)], 'style': ParagraphStyle Msg or None}]."""
    paras = [{"pieces": [], "style": None}]
    for text, run in note_runs(note):
        style = run.msg(2)
        parts = text.split("\n")
        for k, part in enumerate(parts):
            cur = paras[-1]
            if part:
                cur["pieces"].append((part, run))
            if style is not None and cur["style"] is None and (part or k < len(parts) - 1):
                cur["style"] = style
            if k < len(parts) - 1:
                paras.append({"pieces": [], "style": None})
    if not paras[-1]["pieces"]:
        paras.pop()
    return paras


def _inline_html(pieces, resolve):
    out = []
    for text, run in pieces:
        if OBJECT_REPLACEMENT in text:
            info = run.msg(12)
            ident = info.str(1) if info else ""
            uti = info.str(2) if info else ""
            for chunk in re.split(f"({OBJECT_REPLACEMENT})", text):
                if chunk == OBJECT_REPLACEMENT:
                    out.append(resolve(ident, uti) if resolve else "")
                elif chunk:
                    out.append(_styled(chunk, run))
            continue
        out.append(_styled(text, run))
    return "".join(out)


def _styled(text, run):
    s = html.escape(text, quote=False)
    weight = run.int(5)
    font = run.msg(3)
    if font and re.search(r"menlo|courier|mono", font.str(1), re.I):
        s = f'<span style="font-family: var(--mono-font)">{s}</span>'
    if weight in (2, 3):
        s = f"<i>{s}</i>"
    if weight in (1, 3):
        s = f"<b>{s}</b>"
    if run.int(6):
        s = f"<u>{s}</u>"
    if run.int(7):
        s = f"<s>{s}</s>"
    color = run.msg(10)
    if color:
        r, g, b = (round(max(0.0, min(1.0, color.float(k))) * 255) for k in (1, 2, 3))
        s = f'<span style="color: rgb({r}, {g}, {b})">{s}</span>'
    link = run.str(9)
    if link:
        s = f'<a href="{html.escape(link)}">{s}</a>'
    return s


def note_to_html(note, resolve=None):
    """Render a Note message as editor HTML. resolve(identifier, uti) -> HTML for an attachment."""
    paras = _paragraphs(note)
    out = []
    list_stack = []  # [(tag, kind)]
    pre_lines = None
    quote = None

    def close_lists(depth=0):
        while len(list_stack) > depth:
            tag, _ = list_stack.pop()
            out.append(f"</{tag}>")

    def flush_pre():
        nonlocal pre_lines
        if pre_lines is not None:
            out.append("<pre>" + "\n".join(pre_lines) + "</pre>")
            pre_lines = None

    def flush_quote():
        nonlocal quote
        if quote is not None:
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            quote = None

    for para in paras:
        style = para["style"]
        kind = style.int(1, -1) if style is not None and style.has(1) else -1
        inline = _inline_html(para["pieces"], resolve)

        if kind in LIST_STYLES:
            flush_pre()
            flush_quote()
            depth = style.int(4) + 1
            tag = "ol" if kind == STYLE_NUMBERED else "ul"
            cls = {STYLE_DASHED: ' class="dashed"', STYLE_CHECKLIST: ' class="checklist"'}.get(kind, "")
            if len(list_stack) > depth:
                close_lists(depth)
            if len(list_stack) == depth and list_stack[-1][1] != kind:
                close_lists(depth - 1)
            while len(list_stack) < depth:
                out.append(f"<{tag}{cls}>")
                list_stack.append((tag, kind))
            checked = ""
            if kind == STYLE_CHECKLIST:
                cl = style.msg(5)
                if cl and cl.int(2):
                    checked = ' data-checked="true"'
            out.append(f"<li{checked}>{inline or '<br>'}</li>")
            continue

        close_lists()
        if kind == STYLE_MONO:
            flush_quote()
            if pre_lines is None:
                pre_lines = []
            pre_lines.append(re.sub(r"<[^>]+>", "", inline))
            continue
        flush_pre()
        if style is not None and style.int(8):
            if quote is None:
                quote = []
            quote.append(inline)
            continue
        flush_quote()

        stripped = inline.strip()
        if stripped.startswith("<table") and stripped.endswith("</table>"):
            out.append(stripped)
            continue
        tag = {STYLE_TITLE: "h1", STYLE_HEADING: "h2", STYLE_SUBHEADING: "h3"}.get(kind, "div")
        indent = style.int(4) if style is not None and tag == "div" else 0
        attr = f' data-indent="{min(indent, 8)}"' if indent else ""
        out.append(f"<{tag}{attr}>{inline or '<br>'}</{tag}>")

    close_lists()
    flush_pre()
    flush_quote()
    return "".join(out)


def inline_note_html(note, resolve=None):
    """A note rendered for a table cell: paragraphs joined with <br>, no block wrappers."""
    return "<br>".join(_inline_html(p["pieces"], resolve) for p in _paragraphs(note))


def summarize(note_text):
    text = note_text.replace(OBJECT_REPLACEMENT, "")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    tags = sorted({m.group(1).lower() for m in HASHTAG_RE.finditer(text)})
    return {
        "title": (lines[0] if lines else "")[:200],
        "snippet": " ".join(lines[1:])[:240],
        "plain": text[:200000],
        "tags": tags,
    }


# ------------------------------------------------------------------------- tables


def table_to_html(data, resolve=None):
    """Decode a com.apple.notes.table mergeable-data blob into an HTML table."""
    root = Msg(decompress(data))
    obj = root.msg(2)
    payload = obj.msg(3) if obj else None
    if payload is None:
        raise ValueError("not mergeable data")
    entries = payload.msgs(3)
    keys = payload.strs(4)
    types = payload.strs(5)
    uuids = payload.raw_list(6)

    def entry(idx):
        return entries[idx] if 0 <= idx < len(entries) else Msg()

    def object_index(obj_id):
        return obj_id.int(6) if obj_id else -1

    def target_uuid(idx):
        cmap = entry(idx).msg(13)
        if not cmap:
            return None
        first = cmap.msgs(3)
        return first[0].msg(2).int(2) if first and first[0].msg(2) else None

    table = None
    for e in entries:
        cmap = e.msg(13)
        if cmap and 0 <= cmap.int(1) < len(types) and types[cmap.int(1)] == "com.apple.notes.ICTable":
            table = cmap
            break
    if table is None:
        raise ValueError("no ICTable object")

    refs = {}
    for me in table.msgs(3):
        k = me.int(1)
        if 0 <= k < len(keys):
            refs[keys[k]] = object_index(me.msg(2))

    def ordered(idx):
        indices = {}
        oset = entry(idx).msg(16)
        ordering = oset.msg(1) if oset else None
        if ordering is None:
            return indices
        array = ordering.msg(1)
        for n, att in enumerate(array.msgs(2) if array else []):
            uuid = att.bytes(2)
            if uuid in uuids:
                indices[uuids.index(uuid)] = n
        contents = ordering.msg(2)
        for el in contents.msgs(1) if contents else []:
            k = target_uuid(object_index(el.msg(1)))
            v = target_uuid(object_index(el.msg(2)))
            if k in indices and v is not None:
                indices[v] = indices[k]
        return indices

    rows = ordered(refs.get("crRows", -1))
    cols = ordered(refs.get("crColumns", -1))
    n_rows = max(rows.values(), default=-1) + 1
    n_cols = max(cols.values(), default=-1) + 1
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]

    cells = entry(refs.get("cellColumns", -1)).msg(6)
    for col_el in cells.msgs(1) if cells else []:
        col = target_uuid(object_index(col_el.msg(1)))
        col_dict = entry(object_index(col_el.msg(2))).msg(6)
        for row_el in col_dict.msgs(1) if col_dict else []:
            row = target_uuid(object_index(row_el.msg(1)))
            cell = entry(object_index(row_el.msg(2))).msg(10)
            if cell is not None and row in rows and col in cols:
                grid[rows[row]][cols[col]] = inline_note_html(cell, resolve)

    direction = ""
    d_idx = refs.get("crTableColumnDirection")
    if d_idx is not None:
        reg = entry(d_idx).msg(1)
        direction = reg.msg(2).str(4) if reg and reg.msg(2) else ""
    if "RightToLeft" in direction:
        grid = [list(reversed(r)) for r in grid]

    if not grid:
        raise ValueError("empty table")
    body = "".join("<tr>" + "".join(f"<td>{c or '<br>'}</td>" for c in r) + "</tr>" for r in grid)
    return f'<table class="nt-table"><tbody>{body}</tbody></table>'
