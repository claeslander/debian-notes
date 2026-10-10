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


TABLE_STATS = {}  # structural info about the last decoded table (no content), for the import log


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
        """Ordered set -> ({uuid index: position}, live positions). The ordering array is a CRDT and
        keeps deleted items; only those listed in the set's elements dictionary still exist."""
        oset = entry(idx).msg(16)
        ordering = oset.msg(1) if oset else None
        if ordering is None:
            return {}, set()
        array = ordering.msg(1)
        order = []
        for att in array.msgs(2) if array else []:
            uuid = att.bytes(2)
            order.append(uuids.index(uuid) if uuid in uuids else None)
        position = {u: n for n, u in enumerate(order) if u is not None}
        aliases = {}  # value uuid -> array uuid it stands for
        contents = ordering.msg(2)
        for el in contents.msgs(1) if contents else []:
            k = target_uuid(object_index(el.msg(1)))
            v = target_uuid(object_index(el.msg(2)))
            if k is not None and v is not None:
                aliases[v] = k
        live = set()
        elements = oset.msg(2)
        for el in elements.msgs(1) if elements else []:
            for side in (el.msg(1), el.msg(2)):
                u = target_uuid(object_index(side))
                if u is not None:
                    live.add(aliases.get(u, u))
        # Positions over the full ordering; also report which positions are live.
        out = dict(position)
        for v, k in aliases.items():
            if k in position:
                out[v] = position[k]
        live_positions = {position[u] for u in position if not live or u in live}
        return out, live_positions

    stats = TABLE_STATS
    rows, live_rows = ordered(refs.get("crRows", -1))
    cols, live_cols = ordered(refs.get("crColumns", -1))
    stats.clear()
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

    # Drop rows/columns that were deleted in Notes. Anything with content is always kept.
    keep_rows = [i for i in range(n_rows) if i in live_rows or any(grid[i])]
    keep_cols = [j for j in range(n_cols) if j in live_cols or any(grid[i][j] for i in range(n_rows))]
    grid = [[grid[i][j] for j in keep_cols] for i in keep_rows]
    stats.update(rows=len(keep_rows), cols=len(keep_cols), dropped_rows=n_rows - len(keep_rows),
                 dropped_cols=n_cols - len(keep_cols), empty=sum(1 for r in grid for c in r if not c))
    if not grid:
        raise ValueError("empty table")
    body = "".join("<tr>" + "".join(f"<td>{c or '<br>'}</td>" for c in r) + "</tr>" for r in grid)
    return f'<table class="nt-table"><tbody>{body}</tbody></table>'


# --------------------------------------------------------------------- encoding

# Writing a brand-new note. The layout mirrors what Apple's own clients store for a freshly typed
# note (observed in real records): a NoteStoreProto(1: 0, 2: Document(1: 0, 2: 0, 3: Note)), where
# the Note's topotext CRDT is a start sentinel, one substring holding all text from replica 1, and
# an end sentinel; the version vector has one replica clock pair (next char id, next timestamp).

SENTINEL_END = 0xFFFFFFFF


def _ev(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _fi(num, value):
    return _ev(num << 3) + _ev(int(value))


def _fb(num, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _ev(num << 3 | 2) + _ev(len(data)) + data


def _char_id(replica, clock):
    return _fi(1, replica) + _fi(2, clock)


def build_note_store(paragraphs, replica_uuid, timestamp, uuid_factory):
    """paragraphs: [{"style": None|0|1|2|4|100|101|102|103, "indent": int, "checked": bool,
    "runs": [{"text", "bold", "italic", "underline", "strike", "link"}]}]
    Returns (gzip bytes, plain text)."""
    text_parts, runs = [], []
    stamp = int(timestamp)
    for i, para in enumerate(paragraphs):
        style = para.get("style")
        ps = b""
        if style is not None:
            ps += _fi(1, style)
        ps += _fi(3, 1)  # writing direction: left to right, as Apple writes it
        if para.get("indent"):
            ps += _fi(4, para["indent"])
        if style == STYLE_CHECKLIST:
            ps += _fb(5, _fb(1, uuid_factory()) + _fi(2, 1 if para.get("checked") else 0))
        pieces = [r for r in para.get("runs") or [] if r.get("text")]
        if i < len(paragraphs) - 1:
            if pieces:
                pieces[-1] = dict(pieces[-1], text=pieces[-1]["text"] + "\n")
            else:
                pieces = [{"text": "\n"}]
        for r in pieces:
            t = r["text"].replace(OBJECT_REPLACEMENT, "")
            if not t:
                continue
            body = _fi(1, len(t.encode("utf-16-le")) // 2) + _fb(2, ps)
            weight = (1 if r.get("bold") else 0) | (2 if r.get("italic") else 0)
            if weight:
                body += _fi(5, weight)
            if r.get("underline"):
                body += _fi(6, 1)
            if r.get("strike"):
                body += _fi(7, 1)
            if r.get("link"):
                body += _fb(9, r["link"])
            body += _fi(13, stamp)
            runs.append(body)
            text_parts.append(t)
    text = "".join(text_parts)
    n = len(text.encode("utf-16-le")) // 2
    substrings = [
        _fb(1, _char_id(0, 0)) + _fi(2, 0) + _fb(3, _char_id(0, 0)) + _fi(5, 1),
        _fb(1, _char_id(1, 0)) + _fi(2, n) + _fb(3, _char_id(1, 0)) + _fi(5, 2),
        _fb(1, _char_id(0, SENTINEL_END)) + _fi(2, 0) + _fb(3, _char_id(0, SENTINEL_END)),
    ]
    clock = _fb(1, replica_uuid) + _fb(2, _fi(1, n)) + _fb(2, _fi(1, 1))
    note = _fb(2, text) + b"".join(_fb(3, s) for s in substrings) + _fb(4, _fb(1, clock)) + \
        b"".join(_fb(5, r) for r in runs)
    document = _fi(1, 0) + _fi(2, 0) + _fb(3, note)
    return gzip.compress(_fi(1, 0) + _fb(2, document)), text


# ---------------------------------------------------------------------- editing

# Editing an existing note means adding operations to its topotext CRDT, as another device would:
# this app is its own replica. Removed characters stay in the sequence as tombstones; inserted text
# becomes new substrings with our replica's character ids, placed right after the character they
# follow. Attribute runs (formatting) are rewritten for the new visible text.


def _subs(note):
    out = []
    for s in note.msgs(3):
        cid, ts = s.msg(1) or Msg(), s.msg(3) or Msg()
        out.append({"replica": cid.int(1), "clock": cid.int(2), "length": s.int(2),
                    "ts_replica": ts.int(1), "ts_clock": ts.int(2), "tomb": s.int(4), "children": s.raw_list(5)})
    return out


def _chain(subs):
    """Substring indices in document order, following child links from the start sentinel."""
    order, seen, i = [], set(), 0
    while i is not None and 0 <= i < len(subs) and i not in seen:
        order.append(i)
        seen.add(i)
        kids = subs[i]["children"]
        i = kids[0] if kids else None
    if len(order) != len(subs):
        raise ValueError("unsupported substring tree")
    return order


def _clocks(note):
    vt = note.msg(4)
    out = []
    for c in vt.msgs(1) if vt else []:
        out.append([c.bytes(1), [(rc.int(1), rc.int(2)) for rc in c.msgs(2)]])
    return out


def topotext_visible_length(note):
    subs = _subs(note)
    return sum(subs[i]["length"] for i in _chain(subs) if not subs[i]["tomb"])


def _units(text):
    b = text.encode("utf-16-le")
    return [b[k:k + 2] for k in range(0, len(b), 2)]


def _text_and_runs(paragraphs, timestamp, uuid_factory):
    """Shared by build/edit: the note text and encoded attribute runs for a list of paragraphs."""
    text_parts, runs = [], []
    stamp = int(timestamp)
    for i, para in enumerate(paragraphs):
        style = para.get("style")
        ps = b""
        if style is not None:
            ps += _fi(1, style)
        ps += _fi(3, 1)
        if para.get("indent"):
            ps += _fi(4, para["indent"])
        if style == STYLE_CHECKLIST:
            ps += _fb(5, _fb(1, uuid_factory()) + _fi(2, 1 if para.get("checked") else 0))
        pieces = [r for r in para.get("runs") or [] if r.get("text")]
        if i < len(paragraphs) - 1:
            if pieces:
                pieces[-1] = dict(pieces[-1], text=pieces[-1]["text"] + "\n")
            else:
                pieces = [{"text": "\n"}]
        for r in pieces:
            t = r["text"].replace(OBJECT_REPLACEMENT, "")
            if not t:
                continue
            body = _fi(1, len(t.encode("utf-16-le")) // 2) + _fb(2, ps)
            weight = (1 if r.get("bold") else 0) | (2 if r.get("italic") else 0)
            if weight:
                body += _fi(5, weight)
            if r.get("underline"):
                body += _fi(6, 1)
            if r.get("strike"):
                body += _fi(7, 1)
            if r.get("link"):
                body += _fb(9, r["link"])
            body += _fi(13, stamp)
            runs.append(body)
            text_parts.append(t)
    return "".join(text_parts), runs


def edit_note_store(old_blob, paragraphs, replica_uuid, timestamp, uuid_factory):
    """Apply the new content to an existing note as CRDT operations. Returns (blob, text, changed)."""
    root = Msg(decompress(old_blob))
    doc = root.msg(2)
    note = doc.msg(3) if doc else None
    if note is None:
        raise ValueError("not a note document")
    old_text = note.str(2)
    if OBJECT_REPLACEMENT in old_text:
        raise ValueError("notes with attachments, tables or hashtags can't be edited yet")
    subs = _subs(note)
    order = _chain(subs)
    old_units = _units(old_text)
    if topotext_visible_length(note) != len(old_units):
        raise ValueError("note structure doesn't match its text")

    new_text, runs = _text_and_runs(paragraphs, timestamp, uuid_factory)
    new_units = _units(new_text)
    clocks = _clocks(note)
    idx = next((k for k, c in enumerate(clocks) if c[0] == replica_uuid), None)
    if idx is None:
        clocks.append([replica_uuid, [(0, 0), (0, 0)]])
        idx = len(clocks) - 1
    me = idx + 1  # CharID.replica is a 1-based index into the version vector
    next_char = clocks[idx][1][0][0] if clocks[idx][1] else 0
    ts_clock = clocks[idx][1][1][0] if len(clocks[idx][1]) > 1 else 0

    import difflib
    keep = [True] * len(old_units)
    inserts = {}  # old visible index the text goes after (-1 = at the start) -> list of units
    for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, old_units, new_units, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            for k in range(a0, a1):
                keep[k] = False
        if op in ("insert", "replace"):
            inserts.setdefault(a1 - 1 if op == "replace" else a0 - 1, []).extend(new_units[b0:b1])
    changed = bool(inserts) or not all(keep)

    pieces = []

    def insert_after(pos):
        nonlocal next_char
        units = inserts.pop(pos, None)
        if units:
            pieces.append({"replica": me, "clock": next_char, "length": len(units),
                           "ts_replica": me, "ts_clock": ts_clock, "tomb": 0})
            next_char += len(units)

    vi = 0
    for i in order:
        s = subs[i]
        if s["replica"] == 0 and s["clock"] == SENTINEL_END:
            continue
        if s["tomb"] or s["length"] == 0:
            pieces.append(dict(s))  # tombstones and the start sentinel are kept as they are
        if s["replica"] == 0 and s["clock"] == 0:
            insert_after(-1)
            continue
        if s["tomb"] or s["length"] == 0:
            continue
        start = 0
        for k in range(s["length"]):
            last_of_group = k == s["length"] - 1 or keep[vi] != keep[vi + 1] or (vi in inserts)
            if last_of_group:
                alive = keep[vi]
                pieces.append({"replica": s["replica"], "clock": s["clock"] + start, "length": k - start + 1,
                               "ts_replica": s["ts_replica"] if alive else me,
                               "ts_clock": s["ts_clock"] if alive else ts_clock, "tomb": 0 if alive else 1})
                insert_after(vi)
                start = k + 1
            vi += 1
    if inserts:
        raise ValueError("couldn't place every insertion")
    pieces.append({"replica": 0, "clock": SENTINEL_END, "length": 0, "ts_replica": 0, "ts_clock": SENTINEL_END, "tomb": 0})

    enc_subs = []
    for n, p in enumerate(pieces):
        body = _fb(1, _char_id(p["replica"], p["clock"])) + _fi(2, p["length"]) + _fb(3, _char_id(p["ts_replica"], p["ts_clock"]))
        if p["tomb"]:
            body += _fi(4, 1)
        if n < len(pieces) - 1:
            body += _fi(5, n + 1)
        enc_subs.append(body)

    clocks[idx][1] = [(next_char, 0), (ts_clock + 1, 0)]
    enc_clocks = b"".join(
        _fb(1, _fb(1, uid) + b"".join(_fb(2, _fi(1, a) + (_fi(2, b) if b else b"")) for a, b in rcs)) for uid, rcs in clocks)
    new_note = _fb(2, new_text) + b"".join(_fb(3, s) for s in enc_subs) + _fb(4, enc_clocks) + b"".join(_fb(5, r) for r in runs)
    new_doc = _fi(1, doc.int(1)) + _fi(2, doc.int(2)) + _fb(3, new_note)
    blob = gzip.compress(_fi(1, root.int(1)) + _fb(2, new_doc))

    # Self-check: the visible characters must spell exactly the new text.
    check = parse_note_store(blob)
    if check.str(2) != new_text or topotext_visible_length(check) != len(new_units) or \
            sum(r.int(1) for r in check.msgs(5)) != len(new_units):
        raise ValueError("edited note failed its consistency check")
    return blob, new_text, changed
