"""Import notes from iCloud.com using the web session of a sign-in window.

The user signs in on Apple's own page inside a dedicated WebKit view (its cookies live in a
separate store under the data directory). With that session we call the same CloudKit web
service iCloud.com uses, download every record in the Notes zone and convert them locally.
"""

import base64
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

from gi.repository import GLib, Gtk, WebKit2

from . import applenotes as an

SOURCE = "icloud"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
CLIENT_PARAMS = {"clientBuildNumber": "2420Hotfix12", "clientMasteringNumber": "2420Hotfix12"}
NOTES_ZONE = {"zoneName": "Notes", "zoneType": "REGULAR_CUSTOM_ZONE"}
SIGNIN_URL = "https://www.icloud.com/notes/"
AUTH_COOKIE = "X-APPLE-WEBAUTH-TOKEN"
TRASH_FOLDER = "TrashFolder-CloudKit"
DEFAULT_FOLDER = "DefaultFolder-CloudKit"
IMAGE_UTI = re.compile(r"^public\.(jpeg|png|gif|tiff|heic|heif|image|webp)|com\.apple\.(quicktime-image|photos)")


class Cancelled(Exception):
    pass


class NotSignedIn(Exception):
    pass


# ------------------------------------------------------------------------ records


def _field(rec, name):
    f = (rec.get("fields") or {}).get(name)
    return f if isinstance(f, dict) else None


def rec_bytes(rec, *names):
    for name in names:
        f = _field(rec, name)
        if f is None or f.get("value") is None:
            continue
        v = f["value"]
        if isinstance(v, str):
            try:
                return base64.b64decode(v)
            except ValueError:
                return v.encode()
    return None


def rec_text(rec, *names):
    for name in names:
        f = _field(rec, name)
        if f is None or f.get("value") is None:
            continue
        v = f["value"]
        if f.get("type", "").endswith("BYTES") and isinstance(v, str):
            try:
                return base64.b64decode(v).decode("utf-8", "replace")
            except ValueError:
                return v
        if isinstance(v, (str, int, float)):
            return str(v)
    return None


def rec_ref(rec, *names):
    for name in names:
        f = _field(rec, name)
        v = f and f.get("value")
        if isinstance(v, dict) and v.get("recordName"):
            return v["recordName"]
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v[0].get("recordName")
    return None


def rec_refs(rec, name):
    f = _field(rec, name)
    v = f and f.get("value")
    if isinstance(v, list):
        return [x.get("recordName") for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        return [v.get("recordName")]
    return []


def rec_int(rec, name):
    f = _field(rec, name)
    try:
        return int(f["value"]) if f and f.get("value") is not None else 0
    except (TypeError, ValueError):
        return 0


def rec_time(rec, field_name, meta_key):
    f = _field(rec, field_name)
    if f and isinstance(f.get("value"), (int, float)):
        return f["value"] / 1000.0
    meta = rec.get(meta_key) or {}
    if isinstance(meta.get("timestamp"), (int, float)):
        return meta["timestamp"] / 1000.0
    return time.time()


def rec_asset(rec, *names):
    for name in names:
        f = _field(rec, name)
        v = f and f.get("value")
        if isinstance(v, dict) and v.get("downloadURL"):
            return v
    return None


# ------------------------------------------------------------------------- client


class CloudClient:
    """Minimal CloudKit web client authenticated with iCloud.com cookies."""

    def __init__(self, cookies):
        self.cookies = dict(cookies)
        self.client_id = str(uuid.uuid4()).upper()
        self.dsid = None
        self.ck_url = None

    def _request(self, url, body=None, params=None, raw=False, timeout=60):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Origin", "https://www.icloud.com")
        req.add_header("Referer", "https://www.icloud.com/")
        req.add_header("Accept", "*/*")
        if data is not None:
            req.add_header("Content-Type", "text/plain")
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for header in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = header.partition("=")
                    value = rest.split(";", 1)[0]
                    if name.strip():
                        self.cookies[name.strip()] = value
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 421):
                raise NotSignedIn() from exc
            raise
        return payload if raw else json.loads(payload or b"null")

    def validate(self):
        info = self._request("https://setup.icloud.com/setup/ws/1/validate", body=b"null",
                             params=dict(CLIENT_PARAMS, clientId=self.client_id))
        ds = (info or {}).get("dsInfo") or {}
        ck = ((info or {}).get("webservices") or {}).get("ckdatabasews") or {}
        if not ds.get("dsid") or not ck.get("url"):
            raise NotSignedIn()
        self.dsid = ds["dsid"]
        self.ck_url = ck["url"].rstrip("/")
        name = ds.get("fullName") or " ".join(x for x in (ds.get("firstName"), ds.get("lastName")) if x)
        return {"name": name, "appleId": ds.get("appleId") or ds.get("primaryEmail") or ""}

    def notes_records(self, on_progress, cancelled):
        url = f"{self.ck_url}/database/1/com.apple.notes/production/private/changes/zone"
        params = dict(CLIENT_PARAMS, clientId=self.client_id, dsid=self.dsid)
        token = None
        records = []
        while True:
            if cancelled():
                raise Cancelled()
            zone = {"zoneID": NOTES_ZONE}
            if token:
                zone["syncToken"] = token
            res = self._request(url, body={"zones": [zone]}, params=params, timeout=120)
            z = (res.get("zones") or [{}])[0]
            if z.get("serverErrorCode"):
                raise RuntimeError(z.get("reason") or z["serverErrorCode"])
            records.extend(z.get("records") or [])
            on_progress(records)
            token = z.get("syncToken")
            if not z.get("moreComing") or not token:
                return records

    def download(self, asset, filename="file"):
        url = asset["downloadURL"].replace("${f}", urllib.parse.quote(filename or "file"))
        return self._request(url, raw=True, timeout=180)


# --------------------------------------------------------------- sign-in plumbing


class WebSession:
    """The separate, persistent browser profile used for signing in to iCloud."""

    def __init__(self, data_dir):
        self.base = os.path.join(data_dir, "icloud-web")
        os.makedirs(self.base, exist_ok=True)
        self.manager = WebKit2.WebsiteDataManager(
            base_data_directory=os.path.join(self.base, "data"),
            base_cache_directory=os.path.join(self.base, "cache"),
        )
        self.context = WebKit2.WebContext.new_with_website_data_manager(self.manager)
        self.cookie_manager = self.manager.get_cookie_manager()
        self.cookie_manager.set_persistent_storage(os.path.join(self.base, "cookies.sqlite"),
                                                   WebKit2.CookiePersistentStorage.SQLITE)
        self.cookie_manager.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)

    def get_cookies(self, callback):
        def done(mgr, res):
            try:
                cookies = mgr.get_cookies_finish(res) or []
            except GLib.Error:
                cookies = []
            callback({c.get_name(): c.get_value() for c in cookies})

        self.cookie_manager.get_cookies("https://www.icloud.com/", None, done)

    def sign_out(self, callback=None):
        def done(mgr, res):
            try:
                mgr.clear_finish(res)
            except GLib.Error:
                pass
            if callback:
                callback()

        self.manager.clear(WebKit2.WebsiteDataTypes.ALL, 0, None, done)


class SignInWindow(Gtk.Window):
    def __init__(self, parent, session, on_close):
        super().__init__(title="Sign in to iCloud", transient_for=parent, modal=True, destroy_with_parent=True)
        self.set_default_size(560, 760)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        header = Gtk.HeaderBar(title="Sign in to iCloud", subtitle="Your password goes directly to Apple")
        header.set_show_close_button(True)
        self.set_titlebar(header)
        self.view = WebKit2.WebView.new_with_context(session.context)
        self.view.get_settings().set_user_agent(USER_AGENT)
        self.view.get_settings().set_javascript_can_open_windows_automatically(True)
        # Apple's sign-in sometimes opens a pop-up; keep it in this window.
        self.view.connect("create", lambda view, action: (view.load_request(action.get_request()), None)[1])
        self.add(self.view)
        self.view.load_uri(SIGNIN_URL)
        self.connect("destroy", lambda *_: on_close())
        self.show_all()


# ----------------------------------------------------------------------- importer


class ICloudImporter:
    """Drives the iCloud import and reports progress to the page via N.importer.onEvent."""

    def __init__(self, window, store, data_dir):
        self.win = window
        self.store = store
        self.data_dir = data_dir
        self.session = WebSession(data_dir)
        self.client = None
        self.account = None
        self.records = None
        self.signin_window = None
        self.checking = False
        self.cancel_event = threading.Event()
        self.log_path = os.path.join(data_dir, "icloud-import-log.json")

    # ------------------------------------------------------------- events

    def emit(self, kind, **data):
        payload = json.dumps(dict(data, type=kind))
        GLib.idle_add(lambda: (self.win.eval_js(f"window.N && N.importer && N.importer.onEvent({payload})"), False)[1])

    def _on_main(self, fn):
        """Run fn on the GTK main thread (where the SQLite connection lives) and return its result."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        box = {}
        done = threading.Event()

        def call():
            try:
                box["value"] = fn()
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()
            return False

        GLib.idle_add(call)
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _thread(self, target, *args):
        def run():
            try:
                target(*args)
            except Cancelled:
                self.emit("cancelled")
            except NotSignedIn:
                self.client = None
                self.emit("signin-needed", expired=True)
            except Exception as exc:  # report anything unexpected to the import screen
                traceback.print_exc()
                self._log({"error": repr(exc), "trace": traceback.format_exc()})
                self.emit("error", message=_friendly_error(exc))

        threading.Thread(target=run, daemon=True).start()

    def _log(self, entry):
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(entry, time=time.time())) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ sign-in

    def start(self):
        """Check for an existing session; emits signed-in or signin-needed."""
        self.cancel_event.clear()
        self.session.get_cookies(lambda cookies: self._validate(cookies, quiet=True))

    def _validate(self, cookies, quiet=False):
        if AUTH_COOKIE not in cookies:
            if quiet:
                self.emit("signin-needed")
            return

        if self.checking:
            return

        def work():
            try:
                client = CloudClient(cookies)
                try:
                    account = client.validate()
                except NotSignedIn:
                    if quiet:
                        self.emit("signin-needed")
                    return
                self.client, self.account = client, account
                GLib.idle_add(self._close_signin)
                self.emit("signed-in", account=account)
            finally:
                self.checking = False

        self.checking = True
        self._thread(work)

    def open_signin(self):
        if self.signin_window:
            self.signin_window.present()
            return
        self.signin_window = SignInWindow(self.win, self.session, self._signin_closed)
        GLib.timeout_add_seconds(2, self._poll_signin)

    def _poll_signin(self):
        if not self.signin_window:
            return False
        if self.client is None:
            self.session.get_cookies(lambda cookies: self._validate(cookies))
        return self.client is None

    def _close_signin(self):
        if self.signin_window:
            win, self.signin_window = self.signin_window, None
            win.destroy()
        return False

    def _signin_closed(self):
        self.signin_window = None
        if self.client is None:
            self.emit("signin-closed")

    def sign_out(self):
        self.client = None
        self.account = None
        self.records = None
        self.session.sign_out(lambda: self.emit("signed-out"))

    def cancel(self):
        self.cancel_event.set()

    # --------------------------------------------------------------- scan

    def scan(self):
        if self.client is None:
            self.emit("signin-needed")
            return
        self.cancel_event.clear()
        self._thread(self._scan)

    def _scan(self):
        try:
            os.remove(self.log_path)  # one log per run
        except OSError:
            pass
        records = self.client.notes_records(
            lambda recs: self.emit("scan-progress", notes=sum(
                1 for r in recs if r.get("recordType") == "Note" and not r.get("deleted") and not rec_int(r, "Deleted"))),
            self.cancel_event.is_set)
        self.records = {}
        for rec in records:
            if rec.get("deleted") or not rec.get("recordName"):
                continue
            self.records[rec["recordName"]] = rec
        self._log({"scan": _shape(records)})
        self.emit("scan-done", **self._plan())

    def _notes(self):
        return [r for r in self.records.values() if r.get("recordType") == "Note"]

    def _note_folder(self, rec):
        fid = rec_ref(rec, "Folder", "Folders")
        if not fid:
            us = self._user_specific().get(rec["recordName"])
            fid = us and rec_ref(us, "Folder")
        return fid or DEFAULT_FOLDER

    def _user_specific(self):
        if getattr(self, "_us_cache", None) is None or self._us_cache[0] is not self.records:
            self._us_cache = (self.records, {rec_ref(r, "Note"): r for r in self.records.values()
                                             if r.get("recordType") == "Note_UserSpecific"})
        return self._us_cache[1]

    def _is_locked(self, rec):
        fields = rec.get("fields") or {}
        return bool(rec_int(rec, "PasswordProtected") or any(k.startswith("Crypto") for k in fields))

    def _is_deleted(self, rec):
        return bool(rec_int(rec, "Deleted")) or self._note_folder(rec) == TRASH_FOLDER

    def _folders(self):
        folders = {}
        for rec in self.records.values():
            if rec.get("recordType") not in ("Folder", "Folder_UserSpecific") or rec["recordName"] == TRASH_FOLDER \
                    or rec_int(rec, "Deleted"):
                continue
            name = rec_text(rec, "TitleEncrypted", "Title") or ("Notes" if rec["recordName"] == DEFAULT_FOLDER else "Folder")
            folders[rec["recordName"]] = {"id": rec["recordName"], "name": name,
                                          "parentId": rec_ref(rec, "ParentFolder", "Parent")}
        if any(self._note_folder(n) == DEFAULT_FOLDER for n in self._notes()) and DEFAULT_FOLDER not in folders:
            folders[DEFAULT_FOLDER] = {"id": DEFAULT_FOLDER, "name": "Notes", "parentId": None}
        return folders

    def _plan(self):
        folders = self._folders()
        imported = self._on_main(lambda: self.store.imported(SOURCE))
        counts, deleted, already = {}, 0, 0
        locked = sum(1 for r in self.records.values()
                     if r.get("recordType") == "PasswordProtectedNote" and not rec_int(r, "Deleted"))
        for rec in self._notes():
            if self._is_deleted(rec):
                deleted += 1
                continue
            if self._is_locked(rec):
                locked += 1
                continue
            fid = self._note_folder(rec)
            if fid not in folders:
                folders[fid] = {"id": fid, "name": "Notes", "parentId": None}
            counts[fid] = counts.get(fid, 0) + 1
            if rec["recordName"] in imported:
                already += 1
        for f in folders.values():
            f["count"] = counts.get(f["id"], 0)
            if f["parentId"] not in folders:
                f["parentId"] = None
        # Only show folders that hold notes (and their parents); iCloud keeps many system/empty folder records.
        keep = set()
        for fid in counts:
            while fid and fid not in keep and fid in folders:
                keep.add(fid)
                fid = folders[fid]["parentId"]
        return {"folders": [f for f in folders.values() if f["id"] in keep], "total": sum(counts.values()), "locked": locked,
                "deleted": deleted, "alreadyImported": already, "account": self.account}

    # ------------------------------------------------------------- import

    def run_import(self, options):
        if self.records is None:
            self.emit("error", message="Scan your iCloud notes first.")
            return
        self.cancel_event.clear()
        self._thread(self._import, options or {})

    def _import(self, options):
        chosen = set(options.get("folders") or [])
        include_deleted = bool(options.get("includeDeleted"))
        skip_existing = options.get("skipExisting", True)
        keep_folders = options.get("keepFolders", True)
        imported = self._on_main(lambda: self.store.imported(SOURCE))
        folders = self._folders()
        pinned = {rec_ref(r, "Note") for r in self.records.values()
                  if r.get("recordType") == "Note_UserSpecific" and rec_int(r, "IsPinned")}

        todo = []
        for rec in self._notes():
            deleted = self._is_deleted(rec)
            if self._is_locked(rec) or (deleted and not include_deleted):
                continue
            if not deleted and chosen and self._note_folder(rec) not in chosen:
                continue
            modified = rec_time(rec, "ModificationDate", "modified")
            prev = imported.get(rec["recordName"])
            if prev and skip_existing and prev[1] >= modified - 1:
                continue
            todo.append(rec)

        stats = {"notes": 0, "updated": 0, "tables": 0, "attachments": 0, "failed": 0, "skippedAttachments": 0,
                 "folders": 0}
        total = len(todo)
        self.emit("import-progress", done=0, total=total, title="")
        batch = []
        for i, rec in enumerate(todo):
            if self.cancel_event.is_set():
                break
            title = rec_text(rec, "TitleEncrypted", "Title") or "Untitled"
            try:
                item = self._convert(rec, stats)
            except Exception as exc:
                stats["failed"] += 1
                self._log({"note_error": rec["recordName"], "error": repr(exc), "trace": traceback.format_exc()})
                continue
            item["folder_path"] = self._folder_path(rec, folders) if keep_folders else ["iCloud"]
            item["deleted"] = self._is_deleted(rec)
            item["previous"] = imported.get(rec["recordName"], (None,))[0]
            item["pinned"] = rec["recordName"] in pinned
            batch.append(item)
            if len(batch) >= 10 or i == total - 1:
                self._write(batch, stats)
                batch = []
            self.emit("import-progress", done=i + 1, total=total, title=title)
        if batch:
            self._write(batch, stats)
        cancelled = self.cancel_event.is_set()
        self.emit("import-done", stats=stats, cancelled=cancelled)

    def _folder_path(self, rec, folders):
        path, seen = [], set()
        fid = self._note_folder(rec)
        if fid == TRASH_FOLDER:
            fid = DEFAULT_FOLDER
        while fid and fid in folders and fid not in seen:
            seen.add(fid)
            path.insert(0, None if fid == DEFAULT_FOLDER else folders[fid]["name"])
            fid = folders[fid]["parentId"]
        return [p for p in path if p is not None] or []

    def _write(self, items, stats):
        """DB writes happen on the main thread; block this worker until they are done."""
        done = threading.Event()

        def write():
            try:
                cache = {}
                for item in items:
                    key = tuple(item["folder_path"])
                    if key not in cache:
                        parent = None
                        for name in item["folder_path"]:
                            before = len(self.store.folders())
                            parent = self.store.find_or_create_folder(name, parent)
                            stats["folders"] += len(self.store.folders()) - before
                        cache[key] = parent if parent is not None else self.store.default_folder_id()
                    note_id = self.store.save_imported_note(
                        SOURCE, item["id"], cache[key], item["data"], item["created"], item["modified"],
                        note_id=item["previous"])
                    if item["deleted"]:
                        self.store.db.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (time.time(), note_id))
                    elif item.get("pinned"):
                        self.store.db.execute("UPDATE notes SET pinned = 1 WHERE id = ?", (note_id,))
                    stats["updated" if item["previous"] else "notes"] += 1
                self.store.commit()
            finally:
                done.set()
            return False

        GLib.idle_add(write)
        done.wait()

    # --------------------------------------------------------- conversion

    def _convert(self, rec, stats):
        blob = rec_bytes(rec, "TextDataEncrypted", "TextData")
        if not blob:
            raise ValueError("note has no text data")
        note = an.parse_note_store(blob)
        thumbs = []

        def resolve(ident, uti):
            try:
                html = self._attachment_html(ident, uti, stats)
            except Exception as exc:
                self._log({"attachment_error": ident, "uti": uti, "error": repr(exc), "trace": traceback.format_exc()})
                html = None
            if html is None:
                stats["skippedAttachments"] += 1
                return _missing(uti)
            m = re.search(r'<img[^>]+src="([^"]+)"', html)
            if m:
                thumbs.append(m.group(1))
            return html

        body = an.note_to_html(note, resolve)
        data = an.summarize(_resolved_plain(note, self))
        data["body"] = body
        data["thumbnail"] = thumbs[0] if thumbs else None
        return {
            "id": rec["recordName"],
            "data": data,
            "created": rec_time(rec, "CreationDate", "created"),
            "modified": rec_time(rec, "ModificationDate", "modified"),
        }

    def attachment_record(self, ident):
        rec = self.records.get(ident)
        if rec is None:
            # Some records are keyed differently; fall back to a scan by identifier field.
            for r in self.records.values():
                if rec_text(r, "AttachmentIdentifier", "Identifier") == ident:
                    return r
        return rec

    def inline_text(self, ident):
        rec = self.attachment_record(ident)
        return rec_text(rec, "AltTextEncrypted", "AltText", "DisplayTextEncrypted") if rec else None

    def _attachment_html(self, ident, uti, stats):
        rec = self.attachment_record(ident)
        uti = uti or (rec and rec_text(rec, "UTI", "UTIEncrypted", "AttachmentUTIEncrypted", "TypeUTI")) or ""
        if uti.startswith("com.apple.notes.inlinetextattachment") or (rec and rec.get("recordType") in ("InlineAttachment", "Hashtag")):
            text = self.inline_text(ident)
            return _esc(text) if text else ""
        if rec is None:
            return None

        if uti == "com.apple.notes.table":
            data = rec_bytes(rec, "MergeableDataEncrypted", "MergeableData")
            if not data:
                asset = rec_asset(rec, "MergeableDataAsset")
                data = self.client.download(asset, "table") if asset else None
            if not data:
                return None
            stats["tables"] += 1
            html = an.table_to_html(data, lambda i, u: self._attachment_html(i, u, stats) or "")
            self._log({"table": ident, **an.TABLE_STATS})
            return html

        if uti == "public.url":
            url = rec_text(rec, "URLStringEncrypted", "URLString", "URL") or ""
            title = rec_text(rec, "TitleEncrypted", "Title") or url
            return f'<a href="{_esc(url)}">{_esc(title)}</a>' if url else None

        media = self.records.get(rec_ref(rec, "Media") or "")
        asset = rec_asset(media, "Asset") if media else rec_asset(rec, "Asset", "MediaAsset")
        filename = (media and rec_text(media, "FilenameEncrypted", "Filename")) or rec_text(rec, "TitleEncrypted", "Title") or "attachment"

        if asset and IMAGE_UTI.search(uti):
            path = self._save(self.client.download(asset, filename), filename)
            path = _browser_image(path)
            stats["attachments"] += 1
            return f'<img class="nt-img" src="{_esc(GLib.filename_to_uri(path))}" alt="">'

        # Drawings, scans, PDFs and other rich attachments: use the preview image when there is one.
        preview = self._preview_asset(rec)
        if preview and (not asset or re.search(r"drawing|gallery|paper", uti)):
            path = self._save(self.client.download(preview, "preview.png"), (filename or "preview") + ".png")
            stats["attachments"] += 1
            cls = "nt-img nt-sketch" if "drawing" in uti else "nt-img"
            return f'<img class="{cls}" src="{_esc(GLib.filename_to_uri(path))}" alt="">'

        if asset:
            data = self.client.download(asset, filename)
            path = self._save(data, filename)
            stats["attachments"] += 1
            uri = GLib.filename_to_uri(path)
            return (f'<a class="nt-file" contenteditable="false" href="{_esc(uri)}"><span class="nt-file-icon"></span>'
                    f'<span class="nt-file-meta"><span class="nt-file-name">{_esc(filename)}</span>'
                    f'<span class="nt-file-size">{_size(len(data))}</span></span></a>')
        return None

    def _preview_asset(self, rec):
        f = _field(rec, "PreviewImages")
        assets = [a for a in (f.get("value") if f else None) or [] if isinstance(a, dict) and a.get("downloadURL")]
        if assets:
            widths = (_field(rec, "PreviewWidths") or {}).get("value") or []
            if len(widths) == len(assets):
                return max(zip(widths, assets), key=lambda wa: wa[0] or 0)[1]
            return assets[-1]
        return rec_asset(rec, "FallbackImage", "PreviewImage")

    def _save(self, data, filename):
        base = re.sub(r"[^\w.\- ]+", "_", os.path.basename(filename or "attachment")).strip() or "attachment"
        path = os.path.join(self.store.attachments_dir, f"{uuid.uuid4().hex[:12]}-{base}")
        with open(path, "wb") as fh:
            fh.write(data)
        return path


# ------------------------------------------------------------------------ helpers


def _resolved_plain(note, importer):
    """Plain text for search/snippets, with inline hashtags and mentions spelled out."""
    out = []
    for text, run in an.note_runs(note):
        if an.OBJECT_REPLACEMENT in text:
            info = run.msg(12)
            alt = importer.inline_text(info.str(1)) if info else None
            text = text.replace(an.OBJECT_REPLACEMENT, alt or "")
        out.append(text)
    return "".join(out)


def _browser_image(path):
    """WebKitGTK can't show HEIC/TIFF; convert those to PNG when GdkPixbuf has a loader."""
    if not re.search(r"\.(heic|heif|tiff?)$", path, re.I):
        return path
    try:
        from gi.repository import GdkPixbuf
        png = re.sub(r"\.\w+$", ".png", path)
        GdkPixbuf.Pixbuf.new_from_file(path).savev(png, "png", [], [])
        return png
    except Exception:
        return path


def _missing(uti):
    name = {"com.apple.notes.table": "Table", "com.apple.drawing": "Drawing", "com.apple.drawing.2": "Drawing",
            "com.apple.notes.gallery": "Scan", "com.apple.paper": "Drawing"}.get(uti, "Attachment")
    return f'<span class="nt-missing" contenteditable="false">{name} couldn\'t be imported</span>'


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _size(n):
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _shape(records):
    """Record types and field names only (no content) — written to the import log for troubleshooting."""
    shape = {}
    for r in records:
        t = r.get("recordType") or ("<deleted>" if r.get("deleted") else "?")
        entry = shape.setdefault(t, {"count": 0, "fields": {}})
        entry["count"] += 1
        for k, v in (r.get("fields") or {}).items():
            entry["fields"][k] = v.get("type") if isinstance(v, dict) else type(v).__name__
    return shape


def _friendly_error(exc):
    if isinstance(exc, urllib.error.URLError):
        return "Couldn't reach iCloud. Check your internet connection and try again."
    if isinstance(exc, RuntimeError) and "ZONE_NOT_FOUND" in str(exc):
        return "No iCloud Notes were found for this Apple ID."
    if isinstance(exc, RuntimeError) and re.search(r"ACCESS_DENIED|AUTHENTICATION", str(exc)):
        return ("iCloud refused access to your notes. If you use Advanced Data Protection, turn on "
                "“Access iCloud Data on the Web” on your iPhone and try again.")
    return f"Something went wrong: {exc}"
