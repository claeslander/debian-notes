import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gi.repository import GLib  # noqa: E402

from debian_notes import crypto  # noqa: E402
from debian_notes.app import LOCKED_PREFIX, Api  # noqa: E402
from debian_notes.store import Store  # noqa: E402

crypto.derive.__defaults__ = (1000,)  # fast key derivation in tests
SECRET = b"SECRET-PHOTO-BYTES" * 100


class LockingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.api = Api(None, self.store)
        self.api.setLockPassword("pw")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def attachment(self, name="photo.png", data=SECRET):
        return self.api.saveAttachment(name, __import__("base64").b64encode(data).decode())["uri"]

    def note(self, body, title="Bank"):
        return self.store.create_note(self.store.default_folder_id(), body=body, title=title, plain="SECRETPIN")["id"]

    def files(self):
        return sorted(os.listdir(self.store.attachments_dir))

    def disk_contains(self, needle):
        for root, _dirs, names in os.walk(self.tmp.name):
            for name in names:
                with open(os.path.join(root, name), "rb") as fh:
                    if needle in fh.read():
                        return True
        return False

    def test_locking_encrypts_photos_and_leaves_no_plaintext(self):
        uri = self.attachment()
        body = f'<div>SECRETPIN 4821</div><div><img class="nt-img" src="{uri}" alt=""></div>'
        note_id = self.note(body)
        self.store.close()
        self.store = Store(self.tmp.name)  # the plaintext is in notes.db, as with a note from an earlier day
        self.api.store = self.store
        meta = self.api.lockNote(note_id, {"title": "Bank", "body": body})
        self.assertTrue(meta["locked"])
        self.assertFalse(self.disk_contains(b"SECRETPIN"))
        self.assertFalse(self.disk_contains(b"SECRET-PHOTO"))
        self.assertEqual(len(self.files()), 1)
        self.assertTrue(self.files()[0].endswith("-photo.png.locked"))
        opened = self.api.getNote(note_id)["body"]
        self.assertIn("SECRETPIN 4821", opened)
        locked_uri = opened.split('src="')[1].split('"')[0]
        self.assertTrue(locked_uri.startswith(LOCKED_PREFIX))
        self.assertEqual(self.api.read_locked(locked_uri), SECRET)
        self.api.closeLockedNotes()
        with self.assertRaises(PermissionError):
            self.api.read_locked(locked_uri)

    def test_photo_another_note_shows_stays(self):
        uri = self.attachment()
        body = f'<div><img src="{uri}"></div>'
        self.note(body, "Other")
        note_id = self.note(body)
        self.api.lockNote(note_id, {"title": "Bank", "body": body})
        self.assertEqual(len(self.files()), 2)  # the plain one (still used) and the encrypted copy

    def test_attachments_added_to_a_locked_note_are_encrypted(self):
        note_id = self.note("<div>x</div>")
        self.api.lockNote(note_id, {"title": "Bank", "body": "<div>x</div>"})
        uri = self.api.saveAttachment("scan.pdf", "U0VDUkVULVBERg==", note_id)["uri"]
        self.assertTrue(uri.startswith(LOCKED_PREFIX))
        self.assertFalse(self.disk_contains(b"SECRET-PDF"))
        self.assertEqual(self.api.read_locked(uri), b"SECRET-PDF")

    def test_remove_lock_restores_body_and_files(self):
        uri = self.attachment()
        body = f'<div>Hello</div><div><a class="nt-file" href="{uri}">photo</a></div>'
        note_id = self.note(body)
        self.api.lockNote(note_id, {"title": "Bank", "body": body})
        res = self.api.removeLock(note_id)
        self.assertFalse(res["meta"]["locked"])
        self.assertIn("Hello", self.api.getNote(note_id)["body"])
        plain_uri = res["body"].split('href="')[1].split('"')[0]
        self.assertTrue(plain_uri.startswith("file://"))
        with open(GLib.filename_from_uri(plain_uri)[0], "rb") as fh:
            self.assertEqual(fh.read(), SECRET)

    def test_password_change_reencrypts_attachments(self):
        uri = self.attachment()
        body = f'<img src="{uri}">'
        note_id = self.note(body)
        self.api.lockNote(note_id, {"title": "Bank", "body": body})
        self.assertTrue(self.api.changeLockPassword("pw", "new"))
        self.api.closeLockedNotes()
        self.assertFalse(self.api.unlock("pw"))
        self.assertTrue(self.api.unlock("new"))
        locked_uri = self.api.getNote(note_id)["body"].split('src="')[1].split('"')[0]
        self.assertEqual(self.api.read_locked(locked_uri), SECRET)
        self.assertFalse(any(f.endswith(".new") for f in self.files()))

    def test_unlock_encrypts_attachments_of_older_locked_notes(self):
        uri = self.attachment()
        body = f'<img src="{uri}">'
        note_id = self.note(body)
        self.store.set_locked(note_id, crypto.encrypt(self.api.keys, body))  # as locked by an older version
        self.api.closeLockedNotes()
        self.assertTrue(self.disk_contains(b"SECRET-PHOTO"))
        self.assertTrue(self.api.unlock("pw"))
        self.assertFalse(self.disk_contains(b"SECRET-PHOTO"))
        self.assertIn(LOCKED_PREFIX, self.api.getNote(note_id)["body"])


if __name__ == "__main__":
    unittest.main()
