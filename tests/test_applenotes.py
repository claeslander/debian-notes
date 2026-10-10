import gzip
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from debian_notes import applenotes as an  # noqa: E402


# A tiny protobuf encoder to build Apple-format fixtures.
def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def f(num, value):
    if isinstance(value, bool) or isinstance(value, int):
        return varint(num << 3) + varint(int(value))
    if isinstance(value, float):
        return varint(num << 3 | 5) + struct.pack("<f", value)
    if isinstance(value, str):
        value = value.encode()
    return varint(num << 3 | 2) + varint(len(value)) + value


def run(length, style=None, weight=None, link=None, attachment=None, checked=None, indent=None, strike=False):
    body = f(1, length)
    if style is not None or checked is not None or indent is not None:
        ps = b""
        if style is not None:
            ps += f(1, style)
        if indent:
            ps += f(4, indent)
        if checked is not None:
            ps += f(5, f(1, b"uuid") + f(2, 1 if checked else 0))
        body += f(2, ps)
    if weight:
        body += f(5, weight)
    if strike:
        body += f(7, 1)
    if link:
        body += f(9, link)
    if attachment:
        body += f(12, f(1, attachment[0]) + f(2, attachment[1]))
    return body


def note(text, runs):
    return f(2, text) + b"".join(f(5, r) for r in runs)


def note_store(text, runs):
    return gzip.compress(f(2, f(3, note(text, runs))))


def u16(s):
    return len(s.encode("utf-16-le")) // 2


class NoteDecodingTest(unittest.TestCase):
    def test_styles_lists_and_inline(self):
        parts = [
            ("Pasta\n", dict(style=0)),
            ("2 people\n", {}),
            ("Pasta", dict(style=103, checked=True)),
            ("\n", dict(style=103, checked=True)),
            ("Tomatoes\n", dict(style=103, checked=False)),
            ("Boil water\n", dict(style=102)),
            ("Salt it\n", dict(style=102)),
            ("bold", dict(weight=1)),
            (" and ", {}),
            ("link", dict(link="https://example.com")),
            ("\n", {}),
            ("é😀 done\n", dict(strike=True)),
        ]
        text = "".join(p[0] for p in parts)
        data = note_store(text, [run(u16(t), **kw) for t, kw in parts])
        html = an.note_to_html(an.parse_note_store(data))
        self.assertEqual(
            html,
            '<h1>Pasta</h1><div>2 people</div>'
            '<ul class="checklist"><li data-checked="true">Pasta</li><li>Tomatoes</li></ul>'
            '<ol><li>Boil water</li><li>Salt it</li></ol>'
            '<div><b>bold</b> and <a href="https://example.com">link</a></div>'
            '<div><s>é😀 done</s></div>',
        )

    def test_attachment_and_mono(self):
        parts = [
            ("Intro\n", {}),
            ("￼", dict(attachment=("ATT-1", "com.apple.notes.table"))),
            ("\n", {}),
            ("let x = 1\n", dict(style=4)),
            ("let y = 2\n", dict(style=4)),
        ]
        text = "".join(p[0] for p in parts)
        data = note_store(text, [run(u16(t), **kw) for t, kw in parts])
        seen = []

        def resolve(ident, uti):
            seen.append((ident, uti))
            return '<table class="nt-table"><tbody><tr><td>x</td></tr></tbody></table>'

        html = an.note_to_html(an.parse_note_store(data), resolve)
        self.assertEqual(seen, [("ATT-1", "com.apple.notes.table")])
        self.assertIn('<div>Intro</div><table class="nt-table">', html)
        self.assertTrue(html.endswith("<pre>let x = 1\nlet y = 2</pre>"))

    def test_summary(self):
        s = an.summarize("Focaccia\n￼\n5 personen #bakken\n")
        self.assertEqual((s["title"], s["snippet"], s["tags"]), ("Focaccia", "5 personen #bakken", ["bakken"]))


class TableDecodingTest(unittest.TestCase):
    def test_table(self):
        # Mergeable-data layout: entries reference each other by index; rows/columns are ordered
        # sets of UUIDs; cells are a dictionary column -> (row -> Note).
        uuids = [b"R0", b"R1", b"C0", b"C1"]
        keys = ["crRows", "crColumns", "cellColumns"]
        types = ["com.apple.notes.ICTable", "com.apple.CRDT.NSUUID"]
        entries = []

        def add(e):
            entries.append(e)
            return len(entries) - 1

        def oid_index(i):
            return f(6, i)

        def uuid_ref(u):  # custom map whose first entry points at uuid index u
            return add(f(13, f(1, 1) + f(3, f(1, 0) + f(2, f(2, u)))))

        def ordered(us):
            arr = b"".join(f(2, f(1, n) + f(2, uuids[u])) for n, u in enumerate(us))
            return add(f(16, f(1, f(1, arr))))

        cell_text = {(0, 0): "Product", (0, 1): "Amount", (1, 0): "Milk", (1, 1): "2"}
        rows_idx = ordered([0, 1])
        cols_idx = ordered([2, 3])
        col_entries = b""
        for c, cu in enumerate([2, 3]):
            row_entries = b""
            for r, ru in enumerate([0, 1]):
                t = cell_text[(r, c)]
                cell = add(f(10, note(t, [run(u16(t))])))
                row_entries += f(1, f(1, oid_index(uuid_ref(ru))) + f(2, oid_index(cell)))
            col_dict = add(f(6, row_entries))
            col_entries += f(1, f(1, oid_index(uuid_ref(cu))) + f(2, oid_index(col_dict)))
        cells_idx = add(f(6, col_entries))
        table_map = f(1, 0) + f(3, f(1, 0) + f(2, oid_index(rows_idx))) + \
            f(3, f(1, 1) + f(2, oid_index(cols_idx))) + f(3, f(1, 2) + f(2, oid_index(cells_idx)))
        add(f(13, table_map))

        payload = b"".join(f(3, e) for e in entries) + b"".join(f(4, k) for k in keys) + \
            b"".join(f(5, t) for t in types) + b"".join(f(6, u) for u in uuids)
        blob = gzip.compress(f(2, f(3, payload)))
        self.assertEqual(
            an.table_to_html(blob),
            '<table class="nt-table"><tbody><tr><td>Product</td><td>Amount</td></tr>'
            '<tr><td>Milk</td><td>2</td></tr></tbody></table>',
        )


if __name__ == "__main__":
    unittest.main()
