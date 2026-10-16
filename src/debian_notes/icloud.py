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
FOLDER_SOURCE = "icloud-folder"
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


class TokenExpired(Exception):
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

    def __init__(self, cookies, on_change=None):
        self.cookies = dict(cookies)
        self.on_change = on_change  # called with the cookie dict whenever Apple refreshes a cookie
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
                changed = False
                for header in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = header.partition("=")
                    name = name.strip()
                    value = rest.split(";", 1)[0]
                    if not name:
                        continue
                    if not value or re.search(r"max-age=0\b", rest, re.I):
                        changed |= self.cookies.pop(name, None) is not None
                    elif self.cookies.get(name) != value:
                        self.cookies[name] = value
                        changed = True
                if changed and self.on_change:
                    self.on_change(dict(self.cookies))
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

    def notes_records(self, on_progress, cancelled, token=None):
        """All records changed since `token` (everything when None). Returns (records, new_token)."""
        url = f"{self.ck_url}/database/1/com.apple.notes/production/private/changes/zone"
        params = dict(CLIENT_PARAMS, clientId=self.client_id, dsid=self.dsid)
        incremental = token is not None
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
                if incremental and z["serverErrorCode"] in ("CHANGE_TOKEN_EXPIRED", "BAD_REQUEST", "INVALID_ARGUMENTS"):
                    raise TokenExpired()
                raise RuntimeError(z.get("reason") or z["serverErrorCode"])
            records.extend(z.get("records") or [])
            on_progress(records)
            token = z.get("syncToken") or token
            if not z.get("moreComing"):
                return records, token

    def download(self, asset, filename="file"):
        url = asset["downloadURL"].replace("${f}", urllib.parse.quote(filename or "file"))
        return self._request(url, raw=True, timeout=180)


# --------------------------------------------------------------- sign-in plumbing


class WebSession:
    """The separate, persistent browser profile used for signing in to iCloud."""

    def __init__(self, data_dir):
        self.base = os.path.join(data_dir, "icloud-web")
        os.makedirs(self.base, exist_ok=True)
        # The session cookies grant access to the iCloud account: keep them private to this user.
        os.chmod(self.base, 0o700)
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


def web_note_url(record_name):
    """iCloud.com's own address for one of the user's notes."""
    key = base64.b64encode(f"Private::Notes::currentUser::{record_name}".encode()).decode()
    return f"https://www.icloud.com/notes/note/{key}"


class WebNotesWindow(Gtk.Window):
    """iCloud Notes on the web, signed in with the app's own iCloud session: where iCloud notes are edited."""

    def __init__(self, parent, session, on_close):
        super().__init__(title="iCloud Notes", transient_for=parent, destroy_with_parent=True)
        self.set_default_size(1100, 760)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        header = Gtk.HeaderBar(title="iCloud Notes", subtitle="Your changes come back when you close this window")
        header.set_show_close_button(True)
        self.set_titlebar(header)
        self.view = WebKit2.WebView.new_with_context(session.context)
        self.view.get_settings().set_user_agent(USER_AGENT)
        self.view.connect("create", lambda view, action: (view.load_request(action.get_request()), None)[1])
        self.add(self.view)
        self.connect("destroy", lambda *_: on_close())
        self.show_all()

    def open(self, url):
        self.view.load_uri(url)
        self.present()


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
        self.web_window = None
        self.checking = False
        self.cancel_event = threading.Event()
        self.log_path = os.path.join(data_dir, "icloud-import-log.json")
        self.cache_path = os.path.join(data_dir, "icloud-cache.json")
        # Apple rotates its session token; the newest cookies are kept here (private file) so a refreshed
        # session survives restarts instead of falling back to the stale copy in the sign-in profile.
        self.session_path = os.path.join(data_dir, "icloud-web", "session.json")
        self.token = None
        self.busy = False
        self.sync_timer = None
        self.sync_state = {"state": "idle", "last": self.store.get_setting("icloud.lastSync")}

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

    def _thread(self, target, *args, exclusive=False):
        if exclusive:
            if self.busy:
                self.emit("error", message="iCloud sync is running. Try again in a few seconds.")
                return
            self.busy = True

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
            finally:
                if exclusive:
                    self.busy = False

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

        def work():
            try:
                self._ensure_client()
            except NotSignedIn:
                self.emit("signin-needed")
                return
            self.emit("signed-in", account=self.account)

        self._thread(work)

    def _validate(self, cookies, quiet=False):
        if AUTH_COOKIE not in cookies:
            if quiet:
                self.emit("signin-needed")
            return

        if self.checking:
            return

        def work():
            try:
                client = self._client_for(cookies)
                if client is None:
                    if quiet:
                        self.emit("signin-needed")
                    return
                self.client = client
                account = self.account
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

    def note_url(self, note_id):
        """iCloud.com's address of the iCloud note behind a local note."""
        name = next((n for n, st in self.store.import_state(SOURCE).items() if st["note_id"] == note_id and st["present"]),
                    None)
        if name is None:
            raise ValueError("This note isn't in iCloud.")
        return web_note_url(name)

    def open_note_on_web(self, note_id):
        """Open an iCloud note in its own iCloud Notes window to edit it there; pull when the window closes."""
        url = self.note_url(note_id)
        if self.web_window is None:
            self.web_window = WebNotesWindow(self.win, self.session, self._web_closed)
        self.web_window.open(url)

    def _web_closed(self):
        self.web_window = None
        # iCloud.com saves while you type; give the last change a moment to arrive, then bring it here.
        GLib.timeout_add_seconds(3, lambda: (self.sync_now(quiet=True), False)[1])

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
        self.token = None
        for path in (self.cache_path, self.session_path):
            try:
                os.remove(path)
            except OSError:
                pass
        if self.sync_config().get("enabled"):
            self._set_sync_state("signin-needed", message="Sign in to iCloud to keep syncing.")
        self.session.sign_out(lambda: self.emit("signed-out"))

    def cancel(self):
        self.cancel_event.set()

    # --------------------------------------------------------------- scan

    def scan(self):
        if self.client is None:
            self.emit("signin-needed")
            return
        self.cancel_event.clear()
        self._thread(self._scan, exclusive=True)

    def _scan(self):
        try:
            os.remove(self.log_path)  # one log per run
        except OSError:
            pass
        records, token = self.client.notes_records(
            lambda recs: self.emit("scan-progress", notes=sum(
                1 for r in recs if r.get("recordType") == "Note" and not r.get("deleted") and not rec_int(r, "Deleted"))),
            self.cancel_event.is_set)
        self.records = {}
        for rec in records:
            if rec.get("deleted") or not rec.get("recordName"):
                continue
            self.records[rec["recordName"]] = rec
        self.token = token
        self._save_cache()
        self._log({"scan": _shape(records)})
        self.emit("scan-done", **self._plan())

    def _notes(self):
        return [r for r in self.records.values() if r.get("recordType") == "Note"]

    def _copies(self):
        """Record names of Apple's conflict copies. When versions of a note clash, Notes keeps them as extra
        notes with the original's creation date and title. Only one of each group is shown here: the newest,
        or on a tie the one imported here first; the others are skipped."""
        imported = self._on_main(lambda: {k: v["note_id"] for k, v in self.store.import_state(SOURCE).items()
                                          if v["present"]})
        groups = {}
        for rec in self._notes():
            created = ((rec.get("fields") or {}).get("CreationDate") or {}).get("value")
            if not created or self._is_deleted(rec) or self._is_locked(rec):
                continue
            groups.setdefault((created, rec_text(rec, "TitleEncrypted", "Title") or ""), []).append(rec)
        copies = set()
        for recs in groups.values():
            if len(recs) > 1:
                recs.sort(key=lambda r: (-rec_time(r, "ModificationDate", "modified"),
                                         imported.get(r["recordName"], float("inf")), r["recordName"]))
                copies.update(r["recordName"] for r in recs[1:])
        return copies

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
        state = self._on_main(lambda: self.store.import_state(SOURCE))
        live = {k for k, v in state.items() if v["present"] and not v["deleted_at"]}
        counts, new_counts, deleted, already = {}, {}, 0, 0
        locked = sum(1 for r in self.records.values()
                     if r.get("recordType") == "PasswordProtectedNote" and not rec_int(r, "Deleted"))
        copies = self._copies()
        for rec in self._notes():
            if rec["recordName"] in copies:
                continue
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
            if rec["recordName"] in live:
                already += 1
            else:
                new_counts[fid] = new_counts.get(fid, 0) + 1
        for f in folders.values():
            f["count"] = counts.get(f["id"], 0)
            f["newCount"] = new_counts.get(f["id"], 0)
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
        self._thread(self._import, options or {}, exclusive=True)

    def _import(self, options):
        chosen = set(options.get("folders") or [])
        include_deleted = bool(options.get("includeDeleted"))
        skip_existing = options.get("skipExisting", True)
        keep_folders = options.get("keepFolders", True)
        if options.get("sync") is not None:
            all_folders = {f["id"] for f in self._plan()["folders"] if f["count"]}
            cfg = dict(self.sync_config(), enabled=bool(options["sync"]), keepFolders=keep_folders,
                       folders=None if not chosen or chosen >= all_folders else sorted(chosen))
            self._on_main(lambda: self.store.set_setting("icloud.sync", cfg))
            GLib.idle_add(self.schedule)
        state = self._on_main(lambda: self.store.import_state(SOURCE))
        folders = self._folders()
        pinned = {rec_ref(r, "Note") for r in self.records.values()
                  if r.get("recordType") == "Note_UserSpecific" and rec_int(r, "IsPinned")}

        copies = self._copies()
        todo = []
        for rec in self._notes():
            deleted = self._is_deleted(rec)
            if self._is_locked(rec) or (deleted and not include_deleted) or rec["recordName"] in copies:
                continue
            if not deleted and chosen and self._note_folder(rec) not in chosen:
                continue
            modified = rec_time(rec, "ModificationDate", "modified")
            st = state.get(rec["recordName"])
            live = st and st["present"] and not st["deleted_at"]
            if live and skip_existing and (st["modified"] or 0) >= modified - 1:
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
            item["folder"] = self._note_folder(rec)
            item["keep_folders"] = keep_folders
            item["deleted"] = self._is_deleted(rec)
            st = state.get(rec["recordName"])
            item["previous"] = st["note_id"] if st and st["present"] else None
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
        if self.sync_config().get("enabled"):
            self._set_sync_state("idle", last=time.time())

    def _resolve_folder(self, fid, folders, keep_folders, stats=None):
        """Local folder id for an iCloud folder (main thread). iCloud folders are tracked by id, so a
        rename in iCloud renames the local folder too."""
        if not keep_folders:
            return self.store.find_or_create_folder("iCloud")
        if fid == TRASH_FOLDER or fid == DEFAULT_FOLDER or fid not in folders:
            return self.store.default_folder_id()
        info = folders[fid]
        local = self.store.mapping(FOLDER_SOURCE).get(fid)
        if local is not None and self.store.folder_exists(local):
            if self.store.folder_name(local) != info["name"]:
                try:
                    self.store.rename_folder(local, info["name"])
                except ValueError:
                    pass
            return local
        parent = None
        if info["parentId"] and info["parentId"] in folders and info["parentId"] != DEFAULT_FOLDER:
            parent = self._resolve_folder(info["parentId"], folders, keep_folders, stats)
        before = len(self.store.folders())
        local = self.store.find_or_create_folder(info["name"], parent)
        if stats is not None:
            stats["folders"] = stats.get("folders", 0) + len(self.store.folders()) - before
        self.store.set_mapping(FOLDER_SOURCE, fid, local)
        return local

    def _write(self, items, stats):
        """DB writes happen on the main thread; block this worker until they are done."""
        folders = self._folders()

        def write():
            for item in items:
                folder_id = self._resolve_folder(item["folder"], folders, item["keep_folders"], stats)
                note_id = self.store.save_imported_note(
                    SOURCE, item["id"], folder_id, item["data"], item["created"], item["modified"],
                    note_id=item["previous"])
                if item["deleted"]:
                    self.store.db.execute("UPDATE notes SET deleted_at = ?, folder_id = ? WHERE id = ?",
                                          (time.time(), folder_id, note_id))
                else:
                    self.store.db.execute("UPDATE notes SET deleted_at = NULL, folder_id = ?, pinned = ? WHERE id = ?",
                                          (folder_id, 1 if item.get("pinned") else 0, note_id))
                stats["updated" if item["previous"] else "notes"] += 1
            self.store.commit()

        self._on_main(write)

    # -------------------------------------------------------------- sync

    def sync_config(self):
        cfg = self.store.get_setting("icloud.sync") if threading.current_thread() is threading.main_thread() \
            else self._on_main(lambda: self.store.get_setting("icloud.sync"))
        # interval 0 = manual: only sync when the user clicks Sync (the default).
        return dict({"enabled": False, "interval": 0, "keepFolders": True, "folders": None}, **(cfg or {}))

    def set_sync_config(self, changes):
        changes = changes or {}
        cfg = dict(self.sync_config(), **{k: v for k, v in changes.items()
                                          if k in ("enabled", "interval", "keepFolders", "folders")})
        self.store.set_setting("icloud.sync", cfg)
        self.schedule()
        if cfg["enabled"] and changes.get("enabled"):
            self.sync_now()
        elif not cfg["enabled"]:
            self._set_sync_state("off")
        return cfg

    def schedule(self):
        """(Re)start the periodic sync timer according to the settings (main thread)."""
        if self.sync_timer:
            GLib.source_remove(self.sync_timer)
            self.sync_timer = None
        cfg = self.sync_config()
        if not cfg["enabled"]:
            return False
        minutes = int(cfg.get("interval") or 0)
        if minutes <= 0:
            return False  # manual sync only
        self.sync_timer = GLib.timeout_add_seconds(minutes * 60, self._tick)
        return False

    def _tick(self):
        self.sync_now(quiet=True)
        return True

    def sync_now(self, quiet=False):
        """Pull from iCloud. Nothing is ever sent."""
        if self.busy:
            return
        self.busy = True
        self._set_sync_state("syncing", progress="")

        def run():
            try:
                self._sync()
            except NotSignedIn:
                self.client = None
                self._set_sync_state("signin-needed", message="Sign in to iCloud to keep syncing.")
            except urllib.error.URLError:
                self._set_sync_state("offline", message="Can't reach iCloud right now.")
            except Exception as exc:
                traceback.print_exc()
                self._log({"sync_error": repr(exc), "trace": traceback.format_exc()})
                self._set_sync_state("error", message=_friendly_error(exc))
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()

    def _set_sync_state(self, state, **extra):
        self.sync_state = dict(self.sync_state, state=state, message=extra.pop("message", ""), **extra)
        if "last" in extra:
            GLib.idle_add(lambda: (self.store.set_setting("icloud.lastSync", extra["last"]), False)[1])
        payload = json.dumps(self.sync_state)
        GLib.idle_add(lambda: (self.win.eval_js(f"window.N && N.sync && N.sync.onStatus({payload})"), False)[1])

    def _saved_session(self):
        try:
            with open(self.session_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_session(self, cookies):
        tmp = self.session_path + ".tmp"
        try:
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
                json.dump(cookies, fh)
            os.replace(tmp, self.session_path)
        except OSError:
            pass

    def _client_for(self, cookies):
        """A validated client for these cookies, or None."""
        if AUTH_COOKIE not in (cookies or {}):
            return None
        client = CloudClient(cookies, on_change=self._save_session)
        try:
            account = client.validate()
        except NotSignedIn:
            return None
        self._save_session(client.cookies)
        self.account = account
        return client

    def _webkit_cookies(self):
        box, done = {}, threading.Event()

        def got(cookies):
            box["cookies"] = cookies
            done.set()

        GLib.idle_add(lambda: (self.session.get_cookies(got), False)[1])
        done.wait(30)
        return box.get("cookies") or {}

    def _ensure_client(self):
        """Reuse the newest saved session first, then the sign-in profile's cookies."""
        if self.client is not None:
            return
        client = self._client_for(self._saved_session()) or self._client_for(self._webkit_cookies())
        if client is None:
            raise NotSignedIn()
        self.client = client

    def _load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("records") or {}, data.get("token")
        except (OSError, ValueError):
            return None, None

    def _save_cache(self):
        if self.records is None:
            return  # never overwrite the cache with one that wasn't loaded
        tmp = self.cache_path + ".tmp"
        try:
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
                json.dump({"token": self.token, "records": self.records}, fh)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _sync(self):
        self._ensure_client()
        if self.records is None:
            self.records, self.token = self._load_cache()
        full = self.records is None or not self.token
        try:
            delta, token = self.client.notes_records(lambda r: None, lambda: False, None if full else self.token)
        except TokenExpired:
            full = True
            delta, token = self.client.notes_records(lambda r: None, lambda: False, None)
        if full:
            self.records = {}

        removed, touched_notes, content_notes = set(), set(), set()
        self._set_sync_state("syncing", progress=f"{len(delta)} changes")
        for rec in delta:
            name = rec.get("recordName")
            if not name:
                continue
            if rec.get("deleted"):
                removed.add(name)
                self.records.pop(name, None)
                continue
            self.records[name] = rec
            kind = rec.get("recordType")
            if kind == "Note":
                touched_notes.add(name)
            elif kind == "Attachment" or kind == "Media":
                note = rec_ref(rec, "Note")
                if note:
                    content_notes.add(note)
            elif kind == "Note_UserSpecific":
                note = rec_ref(rec, "Note")
                if note:
                    touched_notes.add(note)
        # Media records don't point at their note; find the attachment that uses them.
        changed_media = {r.get("recordName") for r in delta if r.get("recordType") == "Media"}
        if changed_media:
            for rec in self.records.values():
                if rec.get("recordType") == "Attachment" and rec_ref(rec, "Media") in changed_media:
                    note = rec_ref(rec, "Note")
                    if note:
                        content_notes.add(note)
        self.token = token
        if full:
            # Everything arrives in a full resync; attachment records alone don't mean a note changed.
            content_notes = set()

        stats = self._apply_changes(touched_notes | content_notes, content_notes, removed, full)
        self._save_cache()
        self._log({"sync": stats})
        self._set_sync_state("idle", last=time.time(), changes=stats)

    def _apply_changes(self, touched, force_content, removed, full):
        """Mirror iCloud into the local copies of iCloud notes. Those copies are read-only here, so a pull
        never overwrites anything local: notes made here are never touched, and an iCloud note deleted
        here stays deleted."""
        cfg = self.sync_config()
        keep_folders = cfg.get("keepFolders", True)
        allowed = set(cfg["folders"]) if cfg.get("folders") else None
        state = self._on_main(lambda: self.store.import_state(SOURCE))
        folders = self._folders()
        pinned = {rec_ref(r, "Note") for r in self.records.values()
                  if r.get("recordType") == "Note_UserSpecific" and rec_int(r, "IsPinned")}
        stats = {"added": 0, "updated": 0, "removed": 0, "moved": 0, "conflicts": 0, "copies": 0, "folders": 0,
                 "notes": 0, "tables": 0, "attachments": 0, "skippedAttachments": 0, "failed": 0, "changed": []}
        ops = []

        copies = self._copies()
        candidates = set(touched) | set(removed)
        # Every note in iCloud that isn't here yet: also picks up one that a sync missed, or a copy that
        # becomes the one shown when the note it duplicated is deleted.
        candidates |= {r["recordName"] for r in self._notes() if r["recordName"] not in state}
        if full:
            candidates |= {r["recordName"] for r in self._notes()}
        candidates |= {name for name in copies if name in state}
        for name, st in state.items():
            if not st["present"] or st["deleted_at"]:
                continue
            rec = self.records.get(name)
            # Changes that arrived while the note sat in Recently Deleted here (then recovered), and copies
            # edited here by an older version of the app, when iCloud notes were still editable.
            if rec is not None and rec_time(rec, "ModificationDate", "modified") > (st["modified"] or 0) + 1:
                candidates.add(name)
            if (st["updated_at"] or 0) > (st["modified"] or 0) + 1:
                candidates.add(name)
        self._progress = [0, len(candidates)]
        for name in candidates:
            rec = self.records.get(name)
            st = state.get(name)
            if rec is not None and (rec.get("recordType") != "Note" or self._is_locked(rec)):
                continue
            gone = rec is None or self._is_deleted(rec)
            if name in copies:
                # A conflict copy of a note that is already here: don't show it twice.
                if st is not None and st["present"] and not st["deleted_at"]:
                    ops.append(("copy", name, st["note_id"]))
                continue
            if st is None:
                # Not imported yet: pick up new notes in the synced folders.
                if gone or (allowed is not None and self._note_folder(rec) not in allowed):
                    continue
                ops.append(("new", self._item(rec, stats, keep_folders, pinned)))
                continue
            if not st["present"]:
                continue  # deleted permanently on this computer: respect that
            if st["deleted_at"]:
                if gone:
                    ops.append(("forget", name))
                continue  # in Recently Deleted here: leave it alone
            edited_here = (st["updated_at"] or 0) > (st["modified"] or 0) + 1
            if gone:
                # Deleted in iCloud: mirror it and stop tracking. A copy edited here stays as a local note.
                ops.append(("detach" if edited_here else "trash", name, st["note_id"]))
                continue
            remote_modified = rec_time(rec, "ModificationDate", "modified")
            if edited_here or remote_modified > (st["modified"] or 0) + 1 or name in force_content:
                item = self._item(rec, stats, keep_folders, pinned)
                if item is None:
                    continue
                if edited_here:
                    local_plain = self._on_main(lambda nid=st["note_id"]: self.store.note_row(nid)["plain"])
                    if not _same_text(local_plain, item["data"]["plain"]):
                        # Edited here: those edits become a local note, the iCloud version a new copy.
                        ops.append(("conflict", name, st["note_id"], item))
                        continue
                item["previous"] = st["note_id"]
                ops.append(("update", item))
                continue
            ops.append(("meta", st["note_id"], self._note_folder(rec), keep_folders, name in pinned, st))

        ops = [op for op in ops if op[0] != "new" or op[1] is not None]

        def apply():
            now = time.time()
            for op in ops:
                kind = op[0]
                if kind == "new":
                    item = op[1]
                    fid = self._resolve_folder(item["folder"], folders, keep_folders, stats)
                    nid = self.store.save_imported_note(SOURCE, item["id"], fid, item["data"], item["created"],
                                                        item["modified"])
                    if item["pinned"]:
                        self.store.db.execute("UPDATE notes SET pinned = 1 WHERE id = ?", (nid,))
                    stats["added"] += 1
                elif kind == "update":
                    item = op[1]
                    fid = self._resolve_folder(item["folder"], folders, keep_folders, stats)
                    nid = self.store.save_imported_note(SOURCE, item["id"], fid, item["data"], item["created"],
                                                        item["modified"], note_id=item["previous"])
                    self.store.db.execute("UPDATE notes SET folder_id = ?, pinned = ? WHERE id = ?",
                                          (fid, 1 if item["pinned"] else 0, nid))
                    stats["updated"] += 1
                    stats["changed"].append(nid)
                elif kind == "conflict":
                    name, item = op[1], op[3]
                    self.store.forget_import(SOURCE, name)
                    fid = self._resolve_folder(item["folder"], folders, keep_folders, stats)
                    self.store.save_imported_note(SOURCE, item["id"], fid, item["data"], item["created"],
                                                  item["modified"])
                    stats["conflicts"] += 1
                elif kind == "trash":
                    self.store.db.execute("UPDATE notes SET deleted_at = ?, pinned = 0 WHERE id = ?", (now, op[2]))
                    self.store.forget_import(SOURCE, op[1])
                    stats["removed"] += 1
                    stats["changed"].append(op[2])
                elif kind == "copy":
                    self.store.db.execute("UPDATE notes SET deleted_at = ?, pinned = 0 WHERE id = ?", (now, op[2]))
                    self.store.forget_import(SOURCE, op[1])
                    stats["copies"] += 1
                    stats["changed"].append(op[2])
                elif kind == "forget":
                    self.store.forget_import(SOURCE, op[1])
                elif kind == "detach":
                    self.store.forget_import(SOURCE, op[1])
                elif kind == "meta":
                    local_id, remote_folder, keep, remote_pinned, st = op[1:]
                    fid = self._resolve_folder(remote_folder, folders, keep, stats)
                    if fid != st["folder_id"]:
                        self.store.db.execute("UPDATE notes SET folder_id = ? WHERE id = ?", (fid, local_id))
                        stats["moved"] += 1
                    if remote_pinned != st["pinned"]:
                        self.store.db.execute("UPDATE notes SET pinned = ? WHERE id = ?", (1 if remote_pinned else 0, local_id))
                        stats["changed"].append(local_id)
            self.store.commit()

        self._on_main(apply)
        return stats

    def _item(self, rec, stats, keep_folders, pinned):
        if getattr(self, "_progress", None):
            self._progress[0] += 1
            self._set_sync_state("syncing", progress=f"{self._progress[0]} of {self._progress[1]}")
        try:
            item = self._convert(rec, stats)
        except Exception as exc:
            stats["failed"] += 1
            self._log({"note_error": rec["recordName"], "error": repr(exc), "trace": traceback.format_exc()})
            return None
        item.update(folder=self._note_folder(rec), keep_folders=keep_folders, previous=None, deleted=False,
                    pinned=rec["recordName"] in pinned)
        return item

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
            path = self._fetch_asset(asset, filename, image=True)
            stats["attachments"] += 1
            return f'<img class="nt-img" src="{_esc(GLib.filename_to_uri(path))}" alt="">'

        # Drawings, scans, PDFs and other rich attachments: use the preview image when there is one.
        preview = self._preview_asset(rec)
        if preview and (not asset or re.search(r"drawing|gallery|paper", uti)):
            path = self._fetch_asset(preview, (filename or "preview") + ".png")
            stats["attachments"] += 1
            cls = "nt-img nt-sketch" if "drawing" in uti else "nt-img"
            return f'<img class="{cls}" src="{_esc(GLib.filename_to_uri(path))}" alt="">'

        if asset:
            path = self._fetch_asset(asset, filename)
            data_len = os.path.getsize(path)
            stats["attachments"] += 1
            uri = GLib.filename_to_uri(path)
            return (f'<a class="nt-file" contenteditable="false" href="{_esc(uri)}"><span class="nt-file-icon"></span>'
                    f'<span class="nt-file-meta"><span class="nt-file-name">{_esc(filename)}</span>'
                    f'<span class="nt-file-size">{_size(data_len)}</span></span></a>')
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

    def _fetch_asset(self, asset, filename, image=False):
        """Download an iCloud asset once; later syncs reuse the file with the same checksum."""
        index_path = os.path.join(self.data_dir, "icloud-assets.json")
        if getattr(self, "_assets", None) is None:
            try:
                with open(index_path, encoding="utf-8") as fh:
                    self._assets = json.load(fh)
            except (OSError, ValueError):
                self._assets = {}
        key = asset.get("fileChecksum") or asset.get("referenceChecksum")
        known = key and self._assets.get(key)
        if known and os.path.exists(known):
            return known
        path = self._save(self.client.download(asset, filename), filename)
        if image:
            path = _browser_image(path)
        if key:
            self._assets[key] = path
            try:
                with open(index_path, "w", encoding="utf-8") as fh:
                    json.dump(self._assets, fh)
            except OSError:
                pass
        return path

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


def _same_text(a, b):
    norm = lambda s: re.sub(r"\s+", " ", (s or "").replace(an.OBJECT_REPLACEMENT, "")).strip()
    return norm(a) == norm(b)


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
    """Record types and field names only (no content), written to the import log for troubleshooting."""
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
