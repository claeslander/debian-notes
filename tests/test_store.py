import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from debian_notes import crypto  # noqa: E402
from debian_notes.store import Store  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.default = self.store.default_folder_id()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_default_folder_created_once(self):
        self.assertTrue(self.store.first_run)
        self.store.close()
        again = Store(self.tmp.name)
        self.assertFalse(again.first_run)
        self.assertEqual(len(again.folders()), 1)
        self.store = again

    def test_save_and_search(self):
        n = self.store.create_note(self.default)
        self.store.save_note(n["id"], {"title": "Shopping", "snippet": "milk", "body": "<h1>Shopping</h1>",
                                       "plain": "Shopping\nMilk and 100% bread_rolls", "tags": ["Food", "food "]})
        meta = self.store.note_meta(n["id"])
        self.assertEqual(meta["tags"], ["food"])
        self.assertEqual(self.store.search("MILK bread"), [n["id"]])
        self.assertEqual(self.store.search("100%"), [n["id"]])
        self.assertEqual(self.store.search("_r"), [n["id"]])
        self.assertEqual(self.store.search("0_"), [])

    def test_trash_and_recover(self):
        n = self.store.create_note(self.default)
        self.assertIsNotNone(self.store.trash_note(n["id"])["deletedAt"])
        self.assertEqual(self.store.search(""), [])
        self.assertIsNone(self.store.recover_note(n["id"])["deletedAt"])

    def test_purge_old_trash(self):
        n = self.store.create_note(self.default)
        self.store.db.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (time.time() - 31 * 86400, n["id"]))
        self.store.purge_trash()
        with self.assertRaises(KeyError):
            self.store.note_meta(n["id"])

    def test_delete_folder_moves_notes_to_trash(self):
        parent = self.store.create_folder("Work")
        child = self.store.create_folder("Projects", parent["id"])
        n = self.store.create_note(child["id"])
        self.store.delete_folder(parent["id"])
        meta = self.store.note_meta(n["id"])
        self.assertIsNotNone(meta["deletedAt"])
        self.assertEqual(meta["folderId"], self.default)
        self.assertEqual([f["id"] for f in self.store.folders()], [self.default])

    def test_folder_rules(self):
        a = self.store.create_folder("A")
        b = self.store.create_folder("A")
        self.assertEqual(b["name"], "A 2")
        c = self.store.create_folder("C", a["id"])
        with self.assertRaises(ValueError):
            self.store.move_folder(a["id"], c["id"])
        with self.assertRaises(ValueError):
            self.store.delete_folder(self.default)

    def test_locked_note_hides_content(self):
        n = self.store.create_note(self.default)
        self.store.save_note(n["id"], {"title": "Secret", "snippet": "pin", "body": "x", "plain": "pin 1234", "tags": ["a"]})
        meta = self.store.set_locked(n["id"], "payload")
        self.assertTrue(meta["locked"])
        self.assertEqual((meta["snippet"], meta["preview"], meta["tags"]), ("", "", []))
        self.assertEqual(self.store.search("1234"), [])


class CryptoTest(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        salt = crypto.new_salt()
        keys = crypto.derive("hunter2", salt, iterations=1000)
        payload = crypto.encrypt(keys, "<h1>Hëllo</h1>" * 50)
        self.assertEqual(crypto.decrypt(keys, payload), "<h1>Hëllo</h1>" * 50)
        self.assertTrue(crypto.check(keys, crypto.verifier(keys)))
        wrong = crypto.derive("hunter3", salt, iterations=1000)
        self.assertFalse(crypto.check(wrong, crypto.verifier(keys)))
        with self.assertRaises(ValueError):
            crypto.decrypt(wrong, payload)

    def test_nonce_is_fresh(self):
        keys = crypto.derive("pw", crypto.new_salt(), iterations=1000)
        self.assertNotEqual(crypto.encrypt(keys, "same"), crypto.encrypt(keys, "same"))


if __name__ == "__main__":
    unittest.main()


class VersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_edit_snapshots_are_throttled_and_restorable(self):
        n = self.store.create_note(self.store.default_folder_id())["id"]
        self.store.save_note(n, {"title": "v1", "body": "<div>v1</div>"})
        self.store.save_note(n, {"title": "v2", "body": "<div>v2</div>"})   # snapshot of v1 (first edit)
        self.store.save_note(n, {"title": "v3", "body": "<div>v3</div>"})   # within 10 min: no new snapshot
        versions = self.store.versions(n)
        self.assertEqual([v["title"] for v in versions], ["v1"])
        self.assertEqual(self.store.version(versions[0]["id"])["body"], "<div>v1</div>")
        self.store.snapshot_current(n, "before-restore")
        self.assertEqual(self.store.versions(n)[0]["reason"], "before-restore")

    def test_locked_notes_get_no_versions(self):
        n = self.store.create_note(self.store.default_folder_id())["id"]
        self.store.save_note(n, {"title": "secret", "body": "<div>secret</div>"})
        self.store.save_note(n, {"title": "secret 2", "body": "<div>secret 2</div>"})
        self.assertTrue(self.store.versions(n))  # history exists before locking…
        self.store.set_locked(n, "payload")
        self.assertEqual(self.store.versions(n), [])  # …and is wiped when the note is locked
        self.assertIsNone(self.store.snapshot_current(n, "edit"))
        self.store.save_note(n, {"title": "secret"}, locked_payload="payload2")
        self.assertEqual(self.store.versions(n), [])
