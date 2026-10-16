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


def bump(rec, text_parts=None, modified=1800000000000, **fields):
    """A newer version of a note record, as iCloud would send it after an edit on the iPhone."""
    new = note_rec(rec["recordName"], rec["fields"]["Folder"]["value"]["recordName"],
                   text_parts or [("Changed\n", dict(style=0))]) if text_parts is not None else dict(rec, fields=dict(rec["fields"]))
    new["fields"]["ModificationDate"] = {"value": modified, "type": "TIMESTAMP"}
    new["fields"].update(fields)
    return new


class ImporterConversionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        imp = object.__new__(icloud.ICloudImporter)  # skip the WebKit sign-in profile
        imp._set_sync_state = lambda *a, **k: None   # no window to report progress to
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
        local = self.imp._resolve_folder("F2", self.imp._folders(), True)
        folders = {f["id"]: f for f in self.store.folders()}
        self.assertEqual(folders[local]["name"], "Pasta")
        self.assertEqual(folders[folders[local]["parentId"]]["name"], "Cookin")

    def test_save_imported_note_dedupes(self):
        f = self.store.find_or_create_folder("Cookin")
        self.assertEqual(self.store.find_or_create_folder("cookin"), f)
        nid = self.store.save_imported_note("icloud", "N1", f, {"title": "A", "body": "<div>A</div>"}, 1.0, 2.0)
        self.assertEqual(self.store.imported("icloud"), {"N1": (nid, 2.0)})
        same = self.store.save_imported_note("icloud", "N1", f, {"title": "B"}, 1.0, 3.0, note_id=nid)
        self.assertEqual(same, nid)
        self.assertEqual(self.store.note_meta(nid)["title"], "B")


class ReimportTest(ImporterConversionTest):
    def test_trashed_notes_are_importable_and_restored(self):
        self.imp.sync_config = lambda: {"enabled": False}
        stats = {"notes": 0, "updated": 0, "tables": 0, "attachments": 0, "skippedAttachments": 0, "failed": 0, "folders": 0}
        item = self.imp._convert(self.imp.records["N1"], stats)
        item.update(folder="F2", keep_folders=True, deleted=False, previous=None, pinned=False)
        self.imp._write([item], stats)
        nid = self.store.import_state("icloud")["N1"]["note_id"]
        self.assertEqual(self.imp._plan()["alreadyImported"], 1)
        self.store.trash_note(nid)  # the user deletes everything
        plan = self.imp._plan()
        self.assertEqual(plan["alreadyImported"], 0)
        self.assertEqual({f["id"]: f["newCount"] for f in plan["folders"]}["F2"], 1)
        item["previous"] = nid
        self.imp._write([item], stats)
        self.assertIsNone(self.store.note_row(nid)["deleted_at"])
        self.assertEqual(len([n for n in self.store.notes() if not n["deletedAt"]]), 1)


class PullSyncTest(ImporterConversionTest):
    def sync(self, touched=(), removed=(), full=False, force=()):
        return self.imp._apply_changes(set(touched), set(force), set(removed), full)

    def note(self, ext="N1"):
        st = self.store.import_state("icloud").get(ext)
        return st and self.store.note_row(st["note_id"])

    def test_first_sync_adds_notes(self):
        stats = self.sync(full=True)
        self.assertEqual(stats["added"], 1)  # locked and trashed notes are skipped
        self.assertIn("Focaccia", self.note()["body"])

    def test_remote_edit_updates_in_place(self):
        self.sync(full=True)
        nid = self.note()["id"]
        self.imp.records["N1"] = bump(self.imp.records["N1"], [("Focaccia v2\n", dict(style=0))])
        stats = self.sync(touched=["N1"])
        self.assertEqual((stats["updated"], stats["conflicts"]), (1, 0))
        self.assertEqual(self.note()["id"], nid)
        self.assertIn("Focaccia v2", self.note()["body"])

    def test_conflict_keeps_both_versions(self):
        self.sync(full=True)
        local = self.note()
        self.store.db.execute("UPDATE notes SET body = '<h1>Mine</h1>', updated_at = ? WHERE id = ?",
                              (local["updated_at"] + 100, local["id"]))
        self.imp.records["N1"] = bump(self.imp.records["N1"], [("Theirs\n", dict(style=0))])
        stats = self.sync(touched=["N1"])
        self.assertEqual(stats["conflicts"], 1)
        self.assertEqual(self.store.note_row(local["id"])["body"], "<h1>Mine</h1>")
        self.assertIn("Theirs", self.note()["body"])
        self.assertNotEqual(self.note()["id"], local["id"])

    def test_same_text_on_both_sides_is_not_a_conflict(self):
        self.sync(full=True)
        local = self.note()
        new_rec = bump(self.imp.records["N1"], [("Focaccia v2\n", dict(style=0))])
        # The local copy was "edited" (timestamp moved) but already says exactly what iCloud now says.
        self.store.db.execute("UPDATE notes SET plain = ?, updated_at = ? WHERE id = ?",
                              ("Focaccia v2\n", local["updated_at"] + 100, local["id"]))
        self.imp.records["N1"] = new_rec
        stats = self.sync(touched=["N1"])
        self.assertEqual((stats["conflicts"], stats["updated"]), (0, 1))
        self.assertEqual(self.note()["id"], local["id"])

    def test_deleted_on_iphone_goes_to_trash(self):
        self.sync(full=True)
        nid = self.note()["id"]
        self.imp.records["N1"] = bump(self.imp.records["N1"], Deleted={"value": 1, "type": "INT64"})
        stats = self.sync(touched=["N1"])
        self.assertEqual(stats["removed"], 1)
        self.assertIsNotNone(self.store.note_row(nid)["deleted_at"])
        self.assertIsNone(self.note())  # no longer tracked

    def test_tombstone_goes_to_trash(self):
        self.sync(full=True)
        nid = self.note()["id"]
        del self.imp.records["N1"]  # record removed outright
        self.assertEqual(self.sync(removed=["N1"])["removed"], 1)
        self.assertIsNotNone(self.store.note_row(nid)["deleted_at"])

    def test_pin_and_folder_rename(self):
        self.sync(full=True)
        self.imp.records["US1"] = {"recordName": "US1", "recordType": "Note_UserSpecific",
                                   "fields": {"Note": ref("N1"), "IsPinned": {"value": 1, "type": "INT64"}}}
        self.imp.records["F2"]["fields"]["TitleEncrypted"] = enc("Pasta & Risotto")
        self.sync(touched=["N1"])
        self.assertEqual(self.note()["pinned"], 1)
        names = {f["name"] for f in self.store.folders()}
        self.assertIn("Pasta & Risotto", names)
        self.assertNotIn("Pasta", names)

    def test_deleted_here_stays_deleted(self):
        self.sync(full=True)
        nid = self.note()["id"]
        self.store.trash_note(nid)
        self.imp.records["N1"] = bump(self.imp.records["N1"], [("Again\n", {})])
        stats = self.sync(touched=["N1"])
        self.assertEqual((stats["added"], stats["updated"]), (0, 0))
        row = self.store.note_row(nid)
        self.assertIsNotNone(row["deleted_at"])
        self.assertIn("Focaccia", row["body"])
        # Recovered later: the change that arrived in the meantime is picked up on the next pull.
        self.store.recover_note(nid)
        self.assertEqual(self.sync()["updated"], 1)
        self.assertIn("Again", self.store.note_row(nid)["body"])

    def test_local_notes_are_never_touched(self):
        self.sync(full=True)
        mine = self.store.create_note(self.store.default_folder_id(), body="<h1>Mine</h1>")
        before = dict(self.store.note_row(mine["id"]))
        self.imp.records["N1"] = bump(self.imp.records["N1"], [("Focaccia v2\n", dict(style=0))])
        self.sync(touched=["N1"], full=True)
        self.assertEqual(dict(self.store.note_row(mine["id"])), before)
        self.assertFalse(self.store.note_meta(mine["id"])["icloud"])

    def test_icloud_notes_are_marked(self):
        self.sync(full=True)
        nid = self.note()["id"]
        self.assertTrue(self.store.is_icloud(nid))
        self.assertTrue(self.store.note_meta(nid)["icloud"])
        copy = self.store.duplicate_note(nid)  # "Make Local Copy": editable, not tracked
        self.assertFalse(copy["icloud"])
        self.assertTrue(self.store.note_meta(nid)["icloud"])

    def test_old_local_edits_become_a_local_note(self):
        # An older version let you edit iCloud notes; such edits are kept as a local note on the next pull.
        self.sync(full=True)
        local = self.note()
        self.store.db.execute("UPDATE notes SET body = '<h1>Mine</h1>', plain = 'Mine', updated_at = ? WHERE id = ?",
                              (local["updated_at"] + 100, local["id"]))
        stats = self.sync()
        self.assertEqual(stats["conflicts"], 1)
        self.assertFalse(self.store.is_icloud(local["id"]))
        self.assertEqual(self.store.note_row(local["id"])["body"], "<h1>Mine</h1>")
        self.assertIn("Focaccia", self.note()["body"])

    def copy_of(self, name, new_name, text_parts=None, modified=1700000000000):
        rec = bump(self.imp.records[name], text_parts, modified=modified)
        rec["recordName"] = new_name
        rec["fields"]["CreationDate"] = self.imp.records[name]["fields"]["CreationDate"]
        rec["fields"]["TitleEncrypted"] = self.imp.records[name]["fields"]["TitleEncrypted"]
        self.imp.records[new_name] = rec
        return rec

    def live(self):
        return [n for n in self.store.notes() if not n["deletedAt"]]

    def test_conflict_copies_in_icloud_are_shown_once(self):
        # Apple's conflict copies: same creation date and title, same modification date as the original.
        self.sync(full=True)
        original = self.note()["id"]
        self.copy_of("N1", "N1-COPY-A")
        self.copy_of("N1", "N1-COPY-B")
        stats = self.sync(touched=["N1-COPY-A", "N1-COPY-B"])
        self.assertEqual((stats["added"], stats["copies"]), (0, 0))
        self.assertEqual([n["id"] for n in self.live()], [original])  # the one imported first stays
        self.assertEqual(self.sync(full=True)["added"], 0)            # and pulling again adds nothing

    def test_copies_already_imported_are_removed(self):
        self.sync(full=True)
        self.copy_of("N1", "N1-COPY")
        self.imp._copies = lambda: set()  # as before this fix: the copy got imported as a separate note
        self.sync(touched=["N1-COPY"])
        self.assertEqual(len(self.live()), 2)
        del self.imp._copies
        stats = self.sync()
        self.assertEqual(stats["copies"], 1)
        self.assertEqual(len(self.live()), 1)
        self.assertTrue(self.note("N1"))  # the original stays

    def test_newest_version_wins_among_copies(self):
        self.sync(full=True)
        self.copy_of("N1", "N1-NEWER", [("Focaccia v3\n", dict(style=0))], modified=1800000000000)
        self.sync(touched=["N1-NEWER"])
        live = self.live()
        self.assertEqual(len(live), 1)
        self.assertIn("Focaccia v3", self.store.note_row(live[0]["id"])["body"])

    def test_permanently_deleted_here_stays_deleted(self):
        self.sync(full=True)
        self.store.delete_note(self.note()["id"])
        self.imp.records["N1"] = bump(self.imp.records["N1"], [("Again\n", {})])
        self.assertEqual(self.sync(touched=["N1"], full=True)["added"], 0)


class WebLinkTest(unittest.TestCase):
    def test_web_note_url(self):
        # The address iCloud.com itself uses for a note (seen in its address bar).
        self.assertEqual(icloud.web_note_url("777D9AE3-2433-4882-853B-84119C0F34D1"),
                         "https://www.icloud.com/notes/note/"
                         "UHJpdmF0ZTo6Tm90ZXM6OmN1cnJlbnRVc2VyOjo3NzdEOUFFMy0yNDMzLTQ4ODItODUzQi04NDExOUMwRjM0RDE=")


class CacheTest(unittest.TestCase):
    def test_cache_keeps_its_sync_token(self):
        # Regression: loading the cache lazily used to drop the token, forcing a full resync.
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            imp = object.__new__(icloud.ICloudImporter)
            imp.cache_path = tmp + "/c.json"
            with open(imp.cache_path, "w") as fh:
                _json.dump({"token": "TOKEN-1", "records": {"X": {"recordName": "X"}}}, fh)
            imp.records, imp.token = imp._load_cache()
            self.assertEqual((imp.token, list(imp.records)), ("TOKEN-1", ["X"]))
            imp._save_cache()
            with open(imp.cache_path) as fh:
                self.assertEqual(_json.load(fh)["token"], "TOKEN-1")


if __name__ == "__main__":
    unittest.main()
