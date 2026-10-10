import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")

from debian_notes import icloud  # noqa: E402
from debian_notes.store import Store  # noqa: E402
from test_applenotes import note_store, run, u16  # noqa: E402


def enc(text):
    return {"value": base64.b64encode(text.encode() if isinstance(text, str) else text).decode(), "type": "ENCRYPTED_BYTES"}


def ref(name):
    return {"value": {"recordName": name, "action": "VALIDATE"}, "type": "REFERENCE"}


def note_rec(name, folder, parts, **extra):
    text = "".join(p[0] for p in parts)
    blob = note_store(text, [run(u16(t), **kw) for t, kw in parts])
    fields = {"TitleEncrypted": enc(parts[0][0].strip()), "TextDataEncrypted": enc(blob), "Folder": ref(folder),
              "ModificationDate": {"value": 1700000000000, "type": "TIMESTAMP"},
              "CreationDate": {"value": 1690000000000, "type": "TIMESTAMP"}}
    fields.update(extra)
    return {"recordName": name, "recordType": "Note", "fields": fields}


class ImporterConversionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        imp = object.__new__(icloud.ICloudImporter)  # skip the WebKit sign-in profile
        imp.store = self.store
        imp.account = {"name": "Test"}
        imp.client = None
        recs = [
            {"recordName": "DefaultFolder-CloudKit", "recordType": "Folder", "fields": {"TitleEncrypted": enc("Notes")}},
            {"recordName": "F1", "recordType": "Folder", "fields": {"TitleEncrypted": enc("Cookin")}},
            {"recordName": "F2", "recordType": "Folder", "fields": {"TitleEncrypted": enc("Pasta"), "ParentFolder": ref("F1")}},
            note_rec("N1", "F2", [("Focaccia\n", dict(style=0)), ("Bake it #bakken ", {}),
                                  ("￼", dict(attachment=("TAG1", "com.apple.notes.inlinetextattachment.hashtag"))),
                                  ("\n", {})]),
            note_rec("N2", "DefaultFolder-CloudKit", [("Secret\n", {})], CryptoInitializationVector=enc("x")),
            note_rec("N3", "TrashFolder-CloudKit", [("Old\n", {})]),
            {"recordName": "TAG1", "recordType": "InlineAttachment", "fields": {"AltTextEncrypted": enc("#italian")}},
        ]
        imp.records = {r["recordName"]: r for r in recs}
        self.imp = imp

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_plan(self):
        plan = self.imp._plan()
        self.assertEqual((plan["total"], plan["locked"], plan["deleted"]), (1, 1, 1))
        folders = {f["id"]: f for f in plan["folders"]}
        self.assertEqual(folders["F2"]["parentId"], "F1")
        self.assertEqual(folders["F2"]["count"], 1)

    def test_plan_from_worker_thread(self):
        # The real scan runs in a worker thread; SQLite access must hop to the main loop.
        import threading
        from gi.repository import GLib
        loop = GLib.MainLoop()
        result = {}

        def work():
            try:
                result["plan"] = self.imp._plan()
            except Exception as exc:
                result["error"] = exc
            GLib.idle_add(loop.quit)

        threading.Thread(target=work).start()
        GLib.timeout_add_seconds(5, loop.quit)
        loop.run()
        self.assertNotIn("error", result)
        self.assertEqual(result["plan"]["total"], 1)

    def test_convert_and_folder_path(self):
        stats = {"tables": 0, "attachments": 0, "skippedAttachments": 0}
        item = self.imp._convert(self.imp.records["N1"], stats)
        self.assertEqual(item["data"]["body"], "<h1>Focaccia</h1><div>Bake it #bakken #italian</div>")
        self.assertEqual(item["data"]["tags"], ["bakken", "italian"])
        self.assertEqual(item["modified"], 1700000000.0)
        self.assertEqual(self.imp._folder_path(self.imp.records["N1"], self.imp._folders()), ["Cookin", "Pasta"])

    def test_save_imported_note_dedupes(self):
        f = self.store.find_or_create_folder("Cookin")
        self.assertEqual(self.store.find_or_create_folder("cookin"), f)
        nid = self.store.save_imported_note("icloud", "N1", f, {"title": "A", "body": "<div>A</div>"}, 1.0, 2.0)
        self.assertEqual(self.store.imported("icloud"), {"N1": (nid, 2.0)})
        same = self.store.save_imported_note("icloud", "N1", f, {"title": "B"}, 1.0, 3.0, note_id=nid)
        self.assertEqual(same, nid)
        self.assertEqual(self.store.note_meta(nid)["title"], "B")


if __name__ == "__main__":
    unittest.main()
