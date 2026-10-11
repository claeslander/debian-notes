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
        # A new note can't carry attachments: tables go as their text fallback, images are left out.
        pieces = [dict(r, text=r.get("fallback", "")) if r.get("attachment") else r for r in para.get("runs") or []]
        pieces = [r for r in pieces if r.get("text")]
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
    """Substring indices in document order. The substrings form a graph rooted at the start sentinel;
    concurrent edits on different devices create branches (a node with several children), which are
    read depth-first in stored order. The end sentinel, where branches meet again, comes last."""
    end = next((k for k, x in enumerate(subs) if x["replica"] == 0 and x["clock"] == SENTINEL_END), None)
    order, seen, stack = [], set(), [0] if subs else []
    while stack:
        i = stack.pop()
        if i in seen or not 0 <= i < len(subs):
            continue
        seen.add(i)
        if i != end:
            order.append(i)
        stack.extend(reversed(subs[i]["children"]))
    if end is not None:
        order.append(end)
    if len(order) != len(subs):
        raise ValueError("unsupported substring graph")
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


FFFC_UNIT = OBJECT_REPLACEMENT.encode("utf-16-le")
HEADING_STYLES = {STYLE_TITLE, STYLE_HEADING, STYLE_SUBHEADING}


def _para_style(para, uuid_factory):
    style = para.get("style")
    ps = b""
    if style is not None:
        ps += _fi(1, style)
    ps += _fi(3, 1)  # writing direction: left to right, as Apple writes it
    if para.get("indent"):
        ps += _fi(4, para["indent"])
    if style == STYLE_CHECKLIST:
        ps += _fb(5, _fb(1, uuid_factory()) + _fi(2, 1 if para.get("checked") else 0))
    return ps


def _sem(style, indent, checked, weight, underline, strike, link):
    """What a run means, for deciding whether formatting changed (ignores ids and timestamps)."""
    style = None if style in (None, -1, 3) else style
    if style in HEADING_STYLES:
        weight = 0  # headings are bold by definition
    return (style, indent or 0, 1 if (style == STYLE_CHECKLIST and checked) else 0,
            weight or 0, 1 if underline else 0, 1 if strike else 0, link or None)


def _old_tokens(note, inline_text):
    """One token per visible UTF-16 unit of the existing note. Attachments are single tokens: block ones
    (tables, images, files) as "ATT", inline ones (hashtags, mentions, links) by the text they show."""
    units = _units(note.str(2))
    runs_at = []
    for run in note.msgs(5):
        runs_at.extend([run] * run.int(1))
    runs_at.extend([Msg()] * (len(units) - len(runs_at)))
    tokens = []
    for i, u in enumerate(units):
        run = runs_at[i]
        ps = run.msg(2)
        style = ps.int(1, -1) if ps and ps.has(1) else None
        cl = ps.msg(5) if ps else None
        sem = _sem(style, ps.int(4) if ps else 0, cl.int(2) if cl else 0, run.int(5), run.int(6), run.int(7), run.str(9))
        if u == FFFC_UNIT:
            info = run.msg(12)
            alt = inline_text(info.str(1), info.str(2)) if (info and inline_text) else None
            key = ("INL", alt) if alt else "ATT"
            tokens.append({"key": key, "kind": "att", "unit": u, "run": run, "sem": ("ATT",) + sem[:2]})
        else:
            tokens.append({"key": u, "kind": "u", "unit": u, "sem": sem})
    return tokens


def _new_tokens(paragraphs, uuid_factory):
    """Tokens for the edited content. Runs marked {"attachment": ...} stand for an existing attachment."""
    tokens = []
    for i, para in enumerate(paragraphs):
        ps = _para_style(para, uuid_factory)
        style, indent, checked = para.get("style"), para.get("indent") or 0, para.get("checked")
        for r in para.get("runs") or []:
            weight = (1 if r.get("bold") else 0) | (2 if r.get("italic") else 0)
            sem = _sem(style, indent, checked, weight, r.get("underline"), r.get("strike"), r.get("link"))
            spec = (ps, weight, bool(r.get("underline")), bool(r.get("strike")), r.get("link") or None)
            if r.get("attachment"):
                tokens.append({"key": "ATT", "kind": "att", "units": [], "spec": spec, "sem": ("ATT",) + sem[:2]})
                continue
            for u in _units(r.get("text", "").replace(OBJECT_REPLACEMENT, "")):
                tokens.append({"key": u, "kind": "u", "units": [u], "spec": spec, "sem": sem})
        if i < len(paragraphs) - 1:
            sem = _sem(style, indent, checked, 0, 0, 0, None)
            tokens.append({"key": _units("\n")[0], "kind": "u", "units": _units("\n"), "spec": (ps, 0, False, False, None), "sem": sem})
    return tokens


def _match_inline(old, new):
    """Where the page shows an inline attachment as plain text (#tag), fold those characters back into one
    token so the diff keeps the attachment. Matched in order, each after the previous one."""
    cursor = 0
    for ot in old:
        if not (isinstance(ot["key"], tuple) and ot["key"][0] == "INL"):
            continue
        want = _units(ot["key"][1])
        n = len(want)
        for p in range(cursor, len(new) - n + 1):
            if all(new[p + k]["kind"] == "u" and new[p + k]["key"] == want[k] for k in range(n)):
                new[p:p + n] = [{"key": ot["key"], "kind": "att", "units": want, "spec": new[p]["spec"],
                                 "sem": ("ATT",) + new[p]["sem"][:2]}]
                cursor = p + 1
                break
    return new


def edit_note_store(old_blob, paragraphs, replica_uuid, timestamp, uuid_factory, inline_text=None):
    """Apply the new content to an existing note as CRDT operations.

    Attachments in the note (tables, images, files, hashtags, links) are kept where they are: the page sends
    them as attachment markers (or, for inline ones, as their text) and they are matched as single tokens.
    New attachments can't be created and are skipped; removed ones are deleted.
    Returns (blob, text, changed, info)."""
    root = Msg(decompress(old_blob))
    doc = root.msg(2)
    note = doc.msg(3) if doc else None
    if note is None:
        raise ValueError("not a note document")
    old_text = note.str(2)
    subs = _subs(note)
    order = _chain(subs)
    old_units = _units(old_text)
    if topotext_visible_length(note) != len(old_units):
        raise ValueError("note structure doesn't match its text")

    old = _old_tokens(note, inline_text)
    new = _match_inline(old, _new_tokens(paragraphs, uuid_factory))
    stamp = int(timestamp)

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
    final = []    # (unit, run spec, meaning) in the new visible order
    info = {"skippedAttachments": 0, "removedAttachments": 0}
    for op, a0, a1, b0, b1 in difflib.SequenceMatcher(
            None, [t["key"] for t in old], [t["key"] for t in new], autojunk=False).get_opcodes():
        if op == "equal":
            for ot, nt in zip(old[a0:a1], new[b0:b1]):
                if ot["kind"] == "att":
                    final.append((ot["unit"], ("ATT", nt["spec"][0], ot["run"]), ot["sem"][:1] + nt["sem"][1:]))
                else:
                    final.append((ot["unit"], nt["spec"], nt["sem"]))
            continue
        if op in ("delete", "replace"):
            for k in range(a0, a1):
                keep[k] = False
                if old[k]["kind"] == "att":
                    info["removedAttachments"] += 1
        if op in ("insert", "replace"):
            units = []
            for nt in new[b0:b1]:
                if nt["kind"] == "att" and not nt["units"]:
                    info["skippedAttachments"] += 1  # a new image/table/file: can't be created in iCloud
                    continue
                units.extend(nt["units"])  # inline attachments that lost their match go in as plain text
                final.extend((u, nt["spec"], nt["sem"] if nt["kind"] == "u" else _sem(None, 0, 0, 0, 0, 0, None))
                             for u in nt["units"])
            if units:
                inserts.setdefault(a1 - 1 if op == "replace" else a0 - 1, []).extend(units)
    new_units = [u for u, _, _ in final]
    new_text = b"".join(new_units).decode("utf-16-le")
    changed = bool(inserts) or not all(keep) or [m for _, _, m in final] != [t["sem"] for t in old]

    # Attribute runs for the new text: neighbouring characters with the same formatting share a run;
    # every attachment keeps its own run (with its original attachment reference).
    runs = []
    for unit, spec, _ in final:
        if runs and spec[0] != "ATT" and runs[-1][1] == spec:
            runs[-1][0] += 1
        else:
            runs.append([1, spec])
    enc_runs = []
    for length, spec in runs:
        if spec[0] == "ATT":
            _, ps, old_run = spec
            body = _fi(1, 1) + _fb(2, ps)
            if old_run.has(3):
                body += _fb(3, old_run.bytes(3))
            body += _fb(12, old_run.bytes(12)) + _fi(13, stamp)
        else:
            ps, weight, underline, strike, link = spec
            body = _fi(1, length) + _fb(2, ps)
            if weight:
                body += _fi(5, weight)
            if underline:
                body += _fi(6, 1)
            if strike:
                body += _fi(7, 1)
            if link:
                body += _fb(9, link)
            body += _fi(13, stamp)
        enc_runs.append(body)
    runs = enc_runs

    # Nodes of the new graph. Each original substring becomes one or more pieces (split where characters are
    # kept or deleted, or where text is inserted); pieces keep the original links between substrings, so
    # branches from other devices stay exactly as they were.
    nodes = []
    first, last = {}, {}
    # Character id -> UTF-16 unit, for the strict self-check below.
    unit_of, vi0 = {}, 0
    for i in order:
        x = subs[i]
        if not x["tomb"]:
            for k in range(x["length"]):
                unit_of[(x["replica"], x["clock"] + k)] = old_units[vi0]
                vi0 += 1

    def node(**kw):
        nodes.append(dict(kw, children=[]))
        return len(nodes) - 1

    def insertion(pos):
        nonlocal next_char
        units = inserts.pop(pos, None)
        if not units:
            return None
        n = node(replica=me, clock=next_char, length=len(units), ts_replica=me, ts_clock=ts_clock, tomb=0)
        for k, u in enumerate(units):
            unit_of[(me, next_char + k)] = u
        next_char += len(units)
        return n

    vi = 0
    start_insert = None
    for i in order:
        s = subs[i]
        seq = []
        if s["tomb"] or s["length"] == 0:
            seq.append(node(**{k: s[k] for k in ("replica", "clock", "length", "ts_replica", "ts_clock", "tomb")}))
            if s["replica"] == 0 and s["clock"] == 0:
                start_insert = insertion(-1)
        else:
            start = 0
            for k in range(s["length"]):
                if k == s["length"] - 1 or keep[vi] != keep[vi + 1] or vi in inserts:
                    alive = keep[vi]
                    seq.append(node(replica=s["replica"], clock=s["clock"] + start, length=k - start + 1,
                                    ts_replica=s["ts_replica"] if alive else me,
                                    ts_clock=s["ts_clock"] if alive else ts_clock, tomb=0 if alive else 1))
                    ins = insertion(vi)
                    if ins is not None:
                        seq.append(ins)
                    start = k + 1
                vi += 1
        for a_, b_ in zip(seq, seq[1:]):
            nodes[a_]["children"] = [b_]
        first[i], last[i] = seq[0], seq[-1]
    if inserts:
        raise ValueError("couldn't place every insertion")
    for i in order:
        nodes[last[i]]["children"] = [first[c] for c in subs[i]["children"] if c in first]
    if start_insert is not None:
        # Text typed at the very beginning joins the first branch; the start sentinel keeps its other branches.
        start_node = first[order[0]]
        kids = nodes[start_node]["children"]
        nodes[start_insert]["children"] = kids[:1]
        nodes[start_node]["children"] = [start_insert] + kids[1:]

    # Number the nodes in document order (start sentinel first, end sentinel last).
    new_order = _chain(nodes)
    # Strict self-check: spell the visible text from character ids in the new graph.
    spelled = [unit_of.get((nodes[n]["replica"], nodes[n]["clock"] + k))
               for n in new_order if not nodes[n]["tomb"] for k in range(nodes[n]["length"])]
    if spelled != new_units:
        raise ValueError("edited note failed its consistency check")
    index = {old: new for new, old in enumerate(new_order)}
    pieces = []
    for old in new_order:
        p = dict(nodes[old])
        p["children"] = [index[c] for c in p["children"]]
        pieces.append(p)

    enc_subs = []
    for p in pieces:
        body = _fb(1, _char_id(p["replica"], p["clock"])) + _fi(2, p["length"]) + _fb(3, _char_id(p["ts_replica"], p["ts_clock"]))
        if p["tomb"]:
            body += _fi(4, 1)
        for c in p["children"]:
            body += _fi(5, c)
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
    return blob, new_text, changed, info
