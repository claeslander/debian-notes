"""SQLite persistence for folders, notes, tags and settings."""

import json
import os
import sqlite3
import time

TRASH_RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id                  INTEGER PRIMARY KEY,
    folder_id           INTEGER NOT NULL REFERENCES folders(id),
    title               TEXT NOT NULL DEFAULT '',
    snippet             TEXT NOT NULL DEFAULT '',
    body                TEXT NOT NULL DEFAULT '',
    plain               TEXT NOT NULL DEFAULT '',
    thumbnail           TEXT,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    pinned              INTEGER NOT NULL DEFAULT 0,
    locked              INTEGER NOT NULL DEFAULT 0,
    locked_payload      TEXT,
    deleted_at          REAL
);
CREATE TABLE IF NOT EXISTS note_tags (
    note_id  INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
);
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    note_id      INTEGER NOT NULL,
    modified     REAL NOT NULL,
    PRIMARY KEY (source, external_id)
);
CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY,
    note_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    reason      TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    extra       TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_note ON versions(note_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON note_tags(tag);
"""


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.attachments_dir = os.path.join(data_dir, "attachments")
        os.makedirs(self.attachments_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "notes.db"))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self.first_run = self.default_folder_id() is None
        if self.first_run:
            self.db.execute(
                "INSERT INTO folders (name, is_default, created_at) VALUES (?, 1, ?)",
                ("Notes", time.time()),
            )
            self.db.commit()
        self.purge_trash()

    # ---------------------------------------------------------------- folders

    def default_folder_id(self):
        row = self.db.execute("SELECT id FROM folders WHERE is_default = 1").fetchone()
        return row["id"] if row else None

    def folders(self):
        rows = self.db.execute("SELECT * FROM folders ORDER BY is_default DESC, name COLLATE NOCASE")
        return [self._folder(r) for r in rows]

    def create_folder(self, name, parent_id=None):
        cur = self.db.execute(
            "INSERT INTO folders (name, parent_id, created_at) VALUES (?, ?, ?)",
            (self._unique_folder_name(name, parent_id), parent_id, time.time()),
        )
        self.db.commit()
        return self._folder(self.db.execute("SELECT * FROM folders WHERE id = ?", (cur.lastrowid,)).fetchone())

    def rename_folder(self, folder_id, name):
        name = name.strip()
        if not name:
            raise ValueError("Folder name can't be empty")
        self.db.execute("UPDATE folders SET name = ? WHERE id = ? AND is_default = 0", (name, folder_id))
        self.db.commit()

    def move_folder(self, folder_id, parent_id):
        if folder_id == parent_id or (parent_id is not None and folder_id in self._ancestors(parent_id)):
            raise ValueError("A folder can't be moved into itself")
        self.db.execute("UPDATE folders SET parent_id = ? WHERE id = ? AND is_default = 0", (parent_id, folder_id))
        self.db.commit()

    def delete_folder(self, folder_id):
        """Delete a folder and its subfolders; their notes go to Recently Deleted."""
        ids = [folder_id] + self._descendants(folder_id)
        default = self.default_folder_id()
        if default in ids:
            raise ValueError("The default folder can't be deleted")
        marks = ",".join("?" * len(ids))
        now = time.time()
        self.db.execute(
            f"UPDATE notes SET deleted_at = COALESCE(deleted_at, ?), folder_id = ? WHERE folder_id IN ({marks})",
            (now, default, *ids),
        )
        self.db.execute(f"DELETE FROM folders WHERE id IN ({marks})", ids)
        self.db.commit()

    def _unique_folder_name(self, name, parent_id):
        name = (name or "").strip() or "New Folder"
        existing = {
            r["name"].lower()
            for r in self.db.execute("SELECT name FROM folders WHERE parent_id IS ?", (parent_id,))
        }
        candidate, n = name, 2
        while candidate.lower() in existing:
            candidate = f"{name} {n}"
            n += 1
        return candidate

    def _descendants(self, folder_id):
        out, stack = [], [folder_id]
        while stack:
            for r in self.db.execute("SELECT id FROM folders WHERE parent_id = ?", (stack.pop(),)):
                out.append(r["id"])
                stack.append(r["id"])
        return out

    def _ancestors(self, folder_id):
        out = []
        row = self.db.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,)).fetchone()
        while row and row["parent_id"] is not None:
            out.append(row["parent_id"])
            row = self.db.execute("SELECT parent_id FROM folders WHERE id = ?", (row["parent_id"],)).fetchone()
        return out

    @staticmethod
    def _folder(r):
        return {"id": r["id"], "name": r["name"], "parentId": r["parent_id"], "isDefault": bool(r["is_default"])}

    # ------------------------------------------------------------------ notes

    def notes(self):
        tags = {}
        for r in self.db.execute("SELECT note_id, tag FROM note_tags ORDER BY tag"):
            tags.setdefault(r["note_id"], []).append(r["tag"])
        rows = self.db.execute(
            "SELECT id, folder_id, title, snippet, substr(plain, 1, 400) AS preview, thumbnail,"
            " created_at, updated_at, pinned, locked, deleted_at FROM notes"
        )
        return [self._meta(r, tags.get(r["id"], [])) for r in rows]

    def note_meta(self, note_id):
        r = self.db.execute(
            "SELECT id, folder_id, title, snippet, substr(plain, 1, 400) AS preview, thumbnail,"
            " created_at, updated_at, pinned, locked, deleted_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if r is None:
            raise KeyError(f"No note {note_id}")
        tags = [t["tag"] for t in self.db.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]
        return self._meta(r, tags)

    def note_row(self, note_id):
        r = self.db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if r is None:
            raise KeyError(f"No note {note_id}")
        return r

    def create_note(self, folder_id, body="", title="", snippet="", plain="", tags=(), created_at=None):
        now = time.time()
        if folder_id is None or not self.db.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,)).fetchone():
            folder_id = self.default_folder_id()
        cur = self.db.execute(
            "INSERT INTO notes (folder_id, title, snippet, body, plain, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (folder_id, title, snippet, body, plain, created_at or now, created_at or now),
        )
        self._set_tags(cur.lastrowid, tags)
        self.db.commit()
        return self.note_meta(cur.lastrowid)

    def save_note(self, note_id, data, locked_payload=None):
        """Store editor output. For locked notes the body arrives encrypted in locked_payload."""
        now = time.time()
        if locked_payload is None:
            self.maybe_snapshot_before_edit(note_id)
        if locked_payload is not None:
            self.db.execute(
                "UPDATE notes SET title = ?, snippet = '', body = '', plain = '', thumbnail = NULL,"
                " locked_payload = ?, updated_at = ? WHERE id = ?",
                (data.get("title", ""), locked_payload, now, note_id),
            )
            self._set_tags(note_id, ())
        else:
            self.db.execute(
                "UPDATE notes SET title = ?, snippet = ?, body = ?, plain = ?, thumbnail = ?, updated_at = ?"
                " WHERE id = ?",
                (
                    data.get("title", ""),
                    data.get("snippet", ""),
                    data.get("body", ""),
                    data.get("plain", ""),
                    data.get("thumbnail"),
                    now,
                    note_id,
                ),
            )
            self._set_tags(note_id, data.get("tags", ()))
        self.db.commit()
        return self.note_meta(note_id)

    def set_locked(self, note_id, payload):
        """payload=None removes the lock (caller then saves the plaintext body)."""
        if payload is None:
            self.db.execute("UPDATE notes SET locked = 0, locked_payload = NULL WHERE id = ?", (note_id,))
        else:
            self.db.execute(
                "UPDATE notes SET locked = 1, locked_payload = ?, body = '', plain = '', snippet = '',"
                " thumbnail = NULL WHERE id = ?",
                (payload, note_id),
            )
            # Older versions hold the plaintext: a locked note keeps no history.
            self.db.execute("DELETE FROM versions WHERE note_id = ?", (note_id,))
            self._set_tags(note_id, ())
        self.db.commit()
        return self.note_meta(note_id)

    def locked_notes(self):
        return self.db.execute("SELECT id, locked_payload FROM notes WHERE locked = 1").fetchall()

    def update_locked_payload(self, note_id, payload):
        self.db.execute("UPDATE notes SET locked_payload = ? WHERE id = ?", (payload, note_id))

    def commit(self):
        self.db.commit()

    def move_note(self, note_id, folder_id):
        self.db.execute("UPDATE notes SET folder_id = ?, deleted_at = NULL WHERE id = ?", (folder_id, note_id))
        self.db.commit()
        return self.note_meta(note_id)

    def set_pinned(self, note_id, pinned):
        self.db.execute("UPDATE notes SET pinned = ? WHERE id = ?", (1 if pinned else 0, note_id))
        self.db.commit()
        return self.note_meta(note_id)

    def trash_note(self, note_id):
        self.db.execute("UPDATE notes SET deleted_at = ?, pinned = 0 WHERE id = ?", (time.time(), note_id))
        self.db.commit()
        return self.note_meta(note_id)

    def recover_note(self, note_id):
        self.db.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note_id,))
        self.db.commit()
        return self.note_meta(note_id)

    def delete_note(self, note_id):
        self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.db.commit()

    def empty_trash(self):
        self.db.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL")
        self.db.commit()

    def duplicate_note(self, note_id):
        r = self.note_row(note_id)
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO notes (folder_id, title, snippet, body, plain, thumbnail, created_at, updated_at,"
            " locked, locked_payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["folder_id"], r["title"], r["snippet"], r["body"], r["plain"], r["thumbnail"], now, now,
             r["locked"], r["locked_payload"]),
        )
        self.db.execute(
            "INSERT INTO note_tags (note_id, tag) SELECT ?, tag FROM note_tags WHERE note_id = ?",
            (cur.lastrowid, note_id),
        )
        self.db.commit()
        return self.note_meta(cur.lastrowid)

    def search(self, query):
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        clauses = " AND ".join("(lower(title) LIKE ? OR lower(plain) LIKE ?)" for _ in terms)
        params = []
        for t in terms:
            like = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        clauses = clauses.replace("LIKE ?", "LIKE ? ESCAPE '\\'")
        rows = self.db.execute(f"SELECT id FROM notes WHERE deleted_at IS NULL AND {clauses}", params)
        return [r["id"] for r in rows]

    def purge_trash(self):
        cutoff = time.time() - TRASH_RETENTION_DAYS * 86400
        self.db.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
        self.db.commit()

    def _set_tags(self, note_id, tags):
        self.db.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        clean = {str(t).strip().lower()[:64] for t in tags if str(t).strip()}
        self.db.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in clean])

    @staticmethod
    def _meta(r, tags):
        locked = bool(r["locked"])
        return {
            "id": r["id"],
            "folderId": r["folder_id"],
            "title": r["title"],
            "snippet": "" if locked else r["snippet"],
            "preview": "" if locked else (r["preview"] or ""),
            "thumbnail": None if locked else r["thumbnail"],
            "createdAt": r["created_at"],
            "updatedAt": r["updated_at"],
            "pinned": bool(r["pinned"]),
            "locked": locked,
            "deletedAt": r["deleted_at"],
            "tags": tags,
        }

    # ---------------------------------------------------------------- imports

    def find_or_create_folder(self, name, parent_id=None):
        name = (name or "").strip() or "Notes"
        row = self.db.execute(
            "SELECT id FROM folders WHERE parent_id IS ? AND lower(name) = lower(?)", (parent_id, name)
        ).fetchone()
        if row:
            return row["id"]
        return self.create_folder(name, parent_id)["id"]

    def imported(self, source):
        """external_id -> (note_id, modified) for notes that still exist."""
        rows = self.db.execute(
            "SELECT i.external_id, i.note_id, i.modified FROM imports i JOIN notes n ON n.id = i.note_id"
            " WHERE i.source = ?", (source,)
        )
        return {r["external_id"]: (r["note_id"], r["modified"]) for r in rows}

    def save_imported_note(self, source, external_id, folder_id, data, created, modified, note_id=None):
        """Create (or overwrite) a note from an external source, keeping its original dates."""
        fields = (data.get("title", ""), data.get("snippet", ""), data.get("body", ""), data.get("plain", ""),
                  data.get("thumbnail"))
        if note_id is None:
            cur = self.db.execute(
                "INSERT INTO notes (folder_id, title, snippet, body, plain, thumbnail, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (folder_id, *fields, created, modified),
            )
            note_id = cur.lastrowid
        else:
            self.db.execute(
                "UPDATE notes SET title = ?, snippet = ?, body = ?, plain = ?, thumbnail = ?, updated_at = ?"
                " WHERE id = ?",
                (*fields, modified, note_id),
            )
        self._set_tags(note_id, data.get("tags", ()))
        self.db.execute(
            "INSERT INTO imports (source, external_id, note_id, modified) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(source, external_id) DO UPDATE SET note_id = excluded.note_id, modified = excluded.modified",
            (source, external_id, note_id, modified),
        )
        return note_id

    def import_state(self, source):
        """external_id -> local state of every note ever imported from source (also locally deleted ones)."""
        rows = self.db.execute(
            "SELECT i.external_id, i.note_id, i.modified, n.id AS present, n.updated_at, n.deleted_at,"
            " n.folder_id, n.pinned FROM imports i LEFT JOIN notes n ON n.id = i.note_id WHERE i.source = ?",
            (source,),
        )
        return {r["external_id"]: {
            "note_id": r["note_id"], "modified": r["modified"], "present": r["present"] is not None,
            "updated_at": r["updated_at"], "deleted_at": r["deleted_at"], "folder_id": r["folder_id"],
            "pinned": bool(r["pinned"]),
        } for r in rows}

    def forget_import(self, source, external_id):
        self.db.execute("DELETE FROM imports WHERE source = ? AND external_id = ?", (source, external_id))

    def mapping(self, source):
        """Generic external id -> local id map stored in the imports table (used for folders)."""
        return {r["external_id"]: r["note_id"] for r in
                self.db.execute("SELECT external_id, note_id FROM imports WHERE source = ?", (source,))}

    def set_mapping(self, source, external_id, local_id):
        self.db.execute(
            "INSERT INTO imports (source, external_id, note_id, modified) VALUES (?, ?, ?, 0)"
            " ON CONFLICT(source, external_id) DO UPDATE SET note_id = excluded.note_id",
            (source, external_id, local_id),
        )

    def folder_exists(self, folder_id):
        return self.db.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,)).fetchone() is not None

    def folder_name(self, folder_id):
        r = self.db.execute("SELECT name FROM folders WHERE id = ?", (folder_id,)).fetchone()
        return r["name"] if r else None

    # --------------------------------------------------------------- versions

    VERSIONS_PER_NOTE = 100
    EDIT_SNAPSHOT_EVERY = 600  # seconds between automatic snapshots while editing

    def add_version(self, note_id, reason, title, body, extra=None):
        """Keep a copy of a note's content. Never used for locked notes (it would store plaintext)."""
        if not body or not body.strip():
            return None
        last = self.db.execute("SELECT body FROM versions WHERE note_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                               (note_id,)).fetchone()
        if last and last["body"] == body and extra is None:
            return None
        cur = self.db.execute("INSERT INTO versions (note_id, created_at, reason, title, body, extra) VALUES (?, ?, ?, ?, ?, ?)",
                              (note_id, time.time(), reason, title or "", body, extra))
        self.db.execute(
            "DELETE FROM versions WHERE note_id = ? AND id NOT IN"
            " (SELECT id FROM versions WHERE note_id = ? ORDER BY created_at DESC, id DESC LIMIT ?)",
            (note_id, note_id, self.VERSIONS_PER_NOTE))
        return cur.lastrowid

    def snapshot_current(self, note_id, reason):
        """Save the note's current body as a version (skipped for locked notes)."""
        r = self.db.execute("SELECT title, body, locked FROM notes WHERE id = ?", (note_id,)).fetchone()
        if r and not r["locked"]:
            return self.add_version(note_id, reason, r["title"], r["body"])
        return None

    def maybe_snapshot_before_edit(self, note_id):
        r = self.db.execute("SELECT max(created_at) AS t FROM versions WHERE note_id = ?", (note_id,)).fetchone()
        if r["t"] is None or time.time() - r["t"] >= self.EDIT_SNAPSHOT_EVERY:
            self.snapshot_current(note_id, "edit")

    def versions(self, note_id):
        return [{"id": r["id"], "createdAt": r["created_at"], "reason": r["reason"], "title": r["title"]}
                for r in self.db.execute("SELECT id, created_at, reason, title FROM versions WHERE note_id = ?"
                                         " ORDER BY created_at DESC, id DESC", (note_id,))]

    def version(self, version_id):
        r = self.db.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        if r is None:
            raise KeyError(f"No version {version_id}")
        return {"id": r["id"], "noteId": r["note_id"], "createdAt": r["created_at"], "reason": r["reason"],
                "title": r["title"], "body": r["body"]}

    # --------------------------------------------------------------- settings

    def settings(self):
        out = {}
        for r in self.db.execute("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        return out

    def get_setting(self, key, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except ValueError:
            return default

    def set_setting(self, key, value):
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
