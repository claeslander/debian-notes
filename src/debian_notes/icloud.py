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
PUSH_SOURCE = "icloud-push"  # records this app created in iCloud (for the test push feature)
PUSH_FOLDER_NAME = "From Debian"
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

    def _db_url(self, op):
        return f"{self.ck_url}/database/1/com.apple.notes/production/private/records/{op}"

    def modify(self, operations):
        """Run CloudKit record operations; raises with Apple's reason when one fails."""
        res = self._request(self._db_url("modify"), body={"operations": operations, "zoneID": NOTES_ZONE, "atomic": True},
                            params=dict(CLIENT_PARAMS, clientId=self.client_id, dsid=self.dsid))
        records = res.get("records") or []
        failed = [r for r in records if r.get("serverErrorCode")]
        if failed:
            raise RuntimeError("iCloud refused the change: " + "; ".join(
                f"{r.get('serverErrorCode')} {r.get('reason') or ''}".strip() for r in failed))
        return records

    def lookup(self, names):
        res = self._request(self._db_url("lookup"), body={"records": [{"recordName": n} for n in names], "zoneID": NOTES_ZONE},
                            params=dict(CLIENT_PARAMS, clientId=self.client_id, dsid=self.dsid))
        return res.get("records") or []

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

        todo = []
        for rec in self._notes():
            deleted = self._is_deleted(rec)
            if self._is_locked(rec) or (deleted and not include_deleted):
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

    def sync_now(self, quiet=False, outgoing=None):
        """Pull from iCloud; with `outgoing` (local edits/new notes, prepared by the page) also push."""
        if self.busy:
            return
        self.busy = True
        self._set_sync_state("syncing", progress="")

        def run():
            try:
                self._sync(outgoing)
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

    def _ensure_cache(self):
        """Load the record cache together with its sync token (never one without the other)."""
        if self.records is None:
            records, token = self._load_cache()
            self.records, self.token = (records or {}), (token if records is not None else None)

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

    def _sync(self, outgoing=None):
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
        if outgoing:
            self._push_outgoing(outgoing, stats)
        self._log({"sync": stats})
        self._set_sync_state("idle", last=time.time(), changes=stats)

    def _apply_changes(self, touched, force_content, removed, full):
        cfg = self.sync_config()
        keep_folders = cfg.get("keepFolders", True)
        allowed = set(cfg["folders"]) if cfg.get("folders") else None
        state = self._on_main(lambda: self.store.import_state(SOURCE))
        folders = self._folders()
        pinned = {rec_ref(r, "Note") for r in self.records.values()
                  if r.get("recordType") == "Note_UserSpecific" and rec_int(r, "IsPinned")}
        stats = {"added": 0, "updated": 0, "removed": 0, "moved": 0, "conflicts": 0, "folders": 0,
                 "notes": 0, "tables": 0, "attachments": 0, "skippedAttachments": 0, "failed": 0, "changed": []}
        ops = []

        candidates = set(touched) | set(removed)
        if full:
            candidates |= {r["recordName"] for r in self._notes()}
        self._progress = [0, len(candidates)]
        for name in candidates:
            rec = self.records.get(name)
            st = state.get(name)
            if rec is not None and (rec.get("recordType") != "Note" or self._is_locked(rec)):
                continue
            gone = rec is None or self._is_deleted(rec)
            if st is None:
                # Not imported yet: pick up new notes in the synced folders.
                if gone or (allowed is not None and self._note_folder(rec) not in allowed):
                    continue
                ops.append(("new", self._item(rec, stats, keep_folders, pinned)))
                continue
            if not st["present"]:
                continue  # deleted permanently on this computer: respect that
            edited_here = (st["updated_at"] or 0) > (st["modified"] or 0) + 1
            if gone:
                # Deleted in iCloud: mirror it (unless edited here) and stop tracking — there is nothing
                # left in iCloud for a later Push to delete.
                ops.append(("forget", name) if st["deleted_at"] else ("detach" if edited_here else "trash", name, st["note_id"]))
                continue
            remote_modified = rec_time(rec, "ModificationDate", "modified")
            if remote_modified > (st["modified"] or 0) + 1 or name in force_content:
                item = self._item(rec, stats, keep_folders, pinned)
                if edited_here and item is not None:
                    # Only a real conflict when the local text actually differs from the new iCloud text.
                    local_plain = self._on_main(lambda nid=st["note_id"]: self.store.note_row(nid)["plain"])
                    if _same_text(local_plain, item["data"]["plain"]):
                        edited_here = False
                if edited_here:
                    ops.append(("conflict", name, st["note_id"], item))
                else:
                    item["previous"] = st["note_id"]
                    ops.append(("update", item, bool(st["deleted_at"])))
                continue
            if st["deleted_at"]:
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
                    item, was_trashed = op[1], op[2]
                    if item is None:
                        continue
                    fid = self._resolve_folder(item["folder"], folders, keep_folders, stats)
                    self.store.snapshot_current(item["previous"], "before-sync")
                    nid = self.store.save_imported_note(SOURCE, item["id"], fid, item["data"], item["created"],
                                                        item["modified"], note_id=item["previous"])
                    self.store.db.execute("UPDATE notes SET folder_id = ?, pinned = ?, deleted_at = NULL WHERE id = ?",
                                          (fid, 1 if item["pinned"] else 0, nid))
                    stats["updated"] += 1
                    stats["changed"].append(nid)
                elif kind == "conflict":
                    name, local_id, item = op[1], op[2], op[3]
                    if item is None:
                        continue
                    # Keep the version edited here as a normal local note; the iCloud version becomes a new note.
                    self.store.forget_import(SOURCE, name)
                    fid = self._resolve_folder(item["folder"], folders, keep_folders, stats)
                    item["data"]["title"] = item["data"].get("title", "")
                    self.store.save_imported_note(SOURCE, item["id"], fid, item["data"], item["created"],
                                                  item["modified"])
                    stats["conflicts"] += 1
                elif kind == "trash":
                    self.store.db.execute("UPDATE notes SET deleted_at = ?, pinned = 0 WHERE id = ?", (now, op[2]))
                    self.store.forget_import(SOURCE, op[1])
                    self.store.forget_import(PUSH_SOURCE, op[1])
                    stats["removed"] += 1
                    stats["changed"].append(op[2])
                elif kind == "forget":
                    self.store.forget_import(SOURCE, op[1])
                    self.store.forget_import(PUSH_SOURCE, op[1])
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

    # ---------------------------------------------------- test push (experimental)

    def push_note(self, note_id, payload):
        """Create a new iCloud note from a local one, in the "From Debian" folder. Never edits existing notes."""
        if self.busy:
            self._emit_push(note_id, ok=False, message="iCloud sync is running. Try again in a few seconds.")
            return
        self.busy = True

        def run():
            try:
                self._emit_push(note_id, **self._push(note_id, payload))
            except NotSignedIn:
                self.client = None
                self._emit_push(note_id, ok=False, signin=True, message="Sign in to iCloud first.")
            except Exception as exc:
                traceback.print_exc()
                self._log({"push_error": repr(exc), "trace": traceback.format_exc()})
                self._emit_push(note_id, ok=False, message=str(exc))
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()

    def _emit_push(self, note_id, **result):
        payload = json.dumps(dict(result, noteId=note_id))
        GLib.idle_add(lambda: (self.win.eval_js(f"window.N && N.sync && N.sync.onPush({payload})"), False)[1])

    def _push(self, note_id, payload, folder=None):
        self._ensure_client()
        state = self._on_main(lambda: self.store.import_state(SOURCE))
        if any(st["note_id"] == note_id and st["present"] for st in state.values()):
            return {"ok": False, "message": "This note already exists in iCloud."}
        paragraphs = payload.get("paragraphs") or []
        if not any(r.get("text", "").strip() for p in paragraphs for r in p.get("runs", [])):
            return {"ok": False, "message": "This note is empty."}

        folder = folder or self._push_folder()
        now = time.time()
        record_name = str(uuid.uuid4()).upper()
        blob, text = an.build_note_store(paragraphs, uuid.uuid4().bytes, now, lambda: uuid.uuid4().bytes)
        summary = an.summarize(text)
        enc = lambda b: {"value": base64.b64encode(b if isinstance(b, bytes) else b.encode()).decode(), "type": "ENCRYPTED_BYTES"}
        ms = int(now * 1000)
        ref = {"recordName": folder, "action": "VALIDATE"}
        record = {
            "recordName": record_name, "recordType": "Note",
            "fields": {
                "TitleEncrypted": enc(summary["title"]),
                "SnippetEncrypted": enc(summary["snippet"]),
                "TextDataEncrypted": enc(blob),
                "Folder": {"value": ref, "type": "REFERENCE"},
                "Folders": {"value": [ref], "type": "REFERENCE_LIST"},
                "CreationDate": {"value": ms, "type": "TIMESTAMP"},
                "ModificationDate": {"value": ms, "type": "TIMESTAMP"},
                "FoldersModificationDate": {"value": ms, "type": "TIMESTAMP"},
                "Deleted": {"value": 0, "type": "INT64"},
                "AttachmentViewType": {"value": 0, "type": "INT64"},
                "PaperStyleType": {"value": 0, "type": "INT64"},
                "MinimumSupportedNotesVersion": {"value": 0, "type": "INT64"},
            },
        }
        self._log({"push": record_name, "paragraphs": len(paragraphs), "chars": len(text)})
        self.client.modify([{"operationType": "create", "record": record}])

        # Read it back and check iCloud stored exactly what we sent.
        back = self.client.lookup([record_name])
        verified = False
        if back and not back[0].get("serverErrorCode"):
            data = rec_bytes(back[0], "TextDataEncrypted")
            verified = bool(data) and an.plain_text(an.parse_note_store(data)) == text
            self._ensure_cache()
            self.records[record_name] = back[0]
        remote_modified = rec_time(back[0], "ModificationDate", "modified") if back else now

        def remember():
            row = self.store.note_row(note_id)
            self.store.set_mapping(PUSH_SOURCE, record_name, note_id)
            # Track it like an imported note so pull sync brings iPhone edits back; the local copy counts
            # as unedited from this moment.
            self.store.db.execute(
                "INSERT INTO imports (source, external_id, note_id, modified) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(source, external_id) DO UPDATE SET note_id = excluded.note_id, modified = excluded.modified",
                (SOURCE, record_name, note_id, max(remote_modified, row["updated_at"])))
            self.store.commit()

        self._on_main(remember)
        self._save_cache()
        self._log({"push_done": record_name, "verified": verified})
        return {"ok": True, "verified": verified, "folder": PUSH_FOLDER_NAME}

    def _background(self, note_id, work):
        """Run a push-type operation in a worker thread and report it through N.sync.onPush."""
        if self.busy:
            self._emit_push(note_id, ok=False, message="iCloud sync is running. Try again in a few seconds.")
            return
        self.busy = True

        def run():
            try:
                self._emit_push(note_id, **work())
            except NotSignedIn:
                self.client = None
                self._emit_push(note_id, ok=False, signin=True, message="Sign in to iCloud first.")
            except Exception as exc:
                traceback.print_exc()
                self._log({"push_error": repr(exc), "trace": traceback.format_exc()})
                self._emit_push(note_id, ok=False, message=str(exc))
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()

    def _tracked(self, note_id):
        """(record name, import state) of the iCloud note this local note mirrors, or (None, None)."""
        state = self._on_main(lambda: self.store.import_state(SOURCE))
        for name, st in state.items():
            if st["note_id"] == note_id and st["present"]:
                return name, st
        return None, None

    def _replica(self):
        """This app's stable replica id in Apple's note CRDT."""
        def get():
            value = self.store.get_setting("icloud.replica")
            if not value:
                value = base64.b64encode(uuid.uuid4().bytes).decode()
                self.store.set_setting("icloud.replica", value)
            return value
        return base64.b64decode(self._on_main(get))

    def _fresh_record(self, name, st):
        """Latest server copy of a tracked note; refuses when it changed since the last sync."""
        back = self.client.lookup([name])
        if not back or back[0].get("serverErrorCode"):
            raise RuntimeError("This note no longer exists in iCloud.")
        rec = back[0]
        if rec_time(rec, "ModificationDate", "modified") > (st["modified"] or 0) + 1:
            raise RuntimeError("This note changed in iCloud since the last sync. Sync first, then send your changes.")
        if self._is_locked(rec):
            raise RuntimeError("Locked notes can't be sent.")
        return rec

    def push_changes(self, note_id, payload):
        """Send local edits of an iCloud note back as CRDT operations (experimental)."""
        self._background(note_id, lambda: self._push_changes(note_id, payload))

    def _push_changes(self, note_id, payload):
        self._ensure_client()
        name, st = self._tracked(note_id)
        if name is None:
            return {"ok": False, "message": "This note isn't in iCloud yet. Use “Send to iCloud” first."}
        rec = self._fresh_record(name, st)
        old_blob = rec_bytes(rec, "TextDataEncrypted")
        if not old_blob:
            raise RuntimeError("iCloud returned this note without its text.")
        now = time.time()
        blob, text, changed, info = an.edit_note_store(old_blob, payload.get("paragraphs") or [], self._replica(), now,
                                                       lambda: uuid.uuid4().bytes, inline_text=self.inline_display)
        if not changed:
            self._mark_in_sync(note_id, name, rec_time(rec, "ModificationDate", "modified"))
            return {"ok": True, "unchanged": True}

        # Version history: what iCloud had, and what we are about to send.
        before_html = an.note_to_html(an.parse_note_store(old_blob))
        before_title = rec_text(rec, "TitleEncrypted") or ""
        self._on_main(lambda: (
            self.store.add_version(note_id, "before-push", before_title, before_html,
                                   extra=json.dumps({"record": name, "textData": base64.b64encode(old_blob).decode()})),
            self.store.snapshot_current(note_id, "pushed"),
            self.store.commit()))

        summary = an.summarize(text)
        enc = lambda b: {"value": base64.b64encode(b if isinstance(b, bytes) else b.encode()).decode(), "type": "ENCRYPTED_BYTES"}
        update = {"recordName": name, "recordType": rec.get("recordType", "Note"), "recordChangeTag": rec.get("recordChangeTag"),
                  "fields": {"TitleEncrypted": enc(summary["title"]), "SnippetEncrypted": enc(summary["snippet"]),
                             "TextDataEncrypted": enc(blob),
                             "ModificationDate": {"value": int(now * 1000), "type": "TIMESTAMP"}}}
        self._log({"push_edit": name, "chars": len(text), **info})
        self.client.modify([{"operationType": "update", "record": update}])
        res = self._after_push(note_id, name, text)
        res.update(info)
        return res

    def inline_display(self, ident, uti):
        """The text the page shows for an inline attachment (hashtag, mention, link), or None for block ones."""
        if uti and uti.startswith("com.apple.notes.inlinetextattachment"):
            return self.inline_text(ident) or None
        rec = self.attachment_record(ident) if self.records is not None else None
        if rec is not None and rec.get("recordType") in ("InlineAttachment", "Hashtag"):
            return self.inline_text(ident) or None
        if uti == "public.url" and rec is not None:
            url = rec_text(rec, "URLStringEncrypted", "URLString", "URL") or ""
            return (rec_text(rec, "TitleEncrypted", "Title") or url) or None
        return None

    def _mark_in_sync(self, note_id, name, remote_modified):
        def mark():
            row = self.store.note_row(note_id)
            self.store.db.execute("UPDATE imports SET modified = ? WHERE source = ? AND external_id = ?",
                                  (max(remote_modified, row["updated_at"]), SOURCE, name))
            self.store.commit()
        self._on_main(mark)

    def push_trash(self, note_ids):
        """Move the iCloud copies of notes to Recently Deleted (like deleting them on the iPhone)."""
        ids = note_ids if isinstance(note_ids, list) else [note_ids]
        self._background(ids[0] if len(ids) == 1 else None, lambda: self._push_trash(ids))

    def _push_trash(self, note_ids):
        self._ensure_client()
        self._ensure_cache()
        # Look at every mapping, also ones whose local note is already gone (deleted right after this call).
        state = self._on_main(lambda: self.store.import_state(SOURCE))
        names = [name for name, st in state.items() if st["note_id"] in note_ids]
        trash = {"recordName": TRASH_FOLDER, "action": "VALIDATE"}
        ms = int(time.time() * 1000)
        ops = []
        for rec in self.client.lookup(names) if names else []:
            if rec.get("serverErrorCode") or rec_ref(rec, "Folder") == TRASH_FOLDER:
                continue
            ops.append({"operationType": "update", "record": {
                "recordName": rec["recordName"], "recordType": rec.get("recordType", "Note"),
                "recordChangeTag": rec.get("recordChangeTag"),
                "fields": {"Folder": {"value": trash, "type": "REFERENCE"},
                           "Folders": {"value": [trash], "type": "REFERENCE_LIST"},
                           "FoldersModificationDate": {"value": ms, "type": "TIMESTAMP"}}}})
        self._log({"push_trash": [op["record"]["recordName"] for op in ops]})
        if ops:
            self.client.modify(ops)

        def forget():
            # The iCloud copies are in Recently Deleted now; stop tracking them so sync doesn't bring them back.
            for name in names:
                self.store.forget_import(SOURCE, name)
                self.store.forget_import(PUSH_SOURCE, name)
            self.store.commit()

        self._on_main(forget)
        for op in ops:
            (self.records or {}).pop(op["record"]["recordName"], None)
        self._save_cache()
        return {"ok": True, "trashed": len(ops)}

    def _after_push(self, note_id, name, text):
        """Read the record back, verify, and mark the local copy as in sync."""
        back = self.client.lookup([name])
        verified = None
        if back and not back[0].get("serverErrorCode"):
            if text is not None:
                data = rec_bytes(back[0], "TextDataEncrypted")
                verified = bool(data) and an.plain_text(an.parse_note_store(data)) == text
            self._ensure_cache()
            self.records[name] = back[0]
        remote_modified = rec_time(back[0], "ModificationDate", "modified") if back else time.time()

        def mark():
            row = self.store.note_row(note_id)
            self.store.db.execute("UPDATE imports SET modified = ? WHERE source = ? AND external_id = ?",
                                  (max(remote_modified, row["updated_at"]), SOURCE, name))
            self.store.commit()

        self._on_main(mark)
        self._save_cache()
        self._log({"push_done": name, "verified": verified})
        return {"ok": True, "verified": verified, "updated": True}

    # ------------------------------------------------------ two-way sync

    def sync_candidates(self):
        """Local changes a two-way sync would send (main thread). New notes only count when created after
        pushing was enabled and when they live in a folder that exists in iCloud."""
        since = self.store.get_setting("icloud.pushSince")
        if since is None:
            since = time.time()
            self.store.set_setting("icloud.pushSince", since)
        cfg = self.sync_config()
        state = self.store.import_state(SOURCE)
        tracked = {st["note_id"] for st in state.values() if st["present"]}
        edited = [st["note_id"] for st in state.values()
                  if st["present"] and not st["deleted_at"] and (st["updated_at"] or 0) > (st["modified"] or 0) + 1]
        folder_map = {local: remote for remote, local in self.store.mapping(FOLDER_SOURCE).items()}
        if cfg.get("keepFolders", True):
            folder_map.setdefault(self.store.default_folder_id(), DEFAULT_FOLDER)
        else:
            icloud_folder = self.store.find_or_create_folder("iCloud")
            folder_map = {icloud_folder: DEFAULT_FOLDER}
        new = []
        for r in self.store.db.execute("SELECT id, folder_id FROM notes WHERE deleted_at IS NULL AND locked = 0"
                                       " AND created_at > ?", (since,)):
            if r["id"] not in tracked and r["folder_id"] in folder_map:
                new.append({"id": r["id"], "folder": folder_map[r["folder_id"]]})
        rows = {r["id"]: r for r in self.store.db.execute(
            "SELECT id, body, locked FROM notes WHERE id IN (%s)" % ",".join("?" * len(edited + [n["id"] for n in new]) or "0"),
            edited + [n["id"] for n in new])}
        # Notes deleted here (in Recently Deleted or gone) whose iCloud copy is still tracked.
        titles = {name: (rec_text(rec, "TitleEncrypted") or "") for name, rec in (self.records or {}).items()} \
            if self.records is not None else {}
        deleted = []
        for name, st in state.items():
            rec = (self.records or {}).get(name)
            complete = self.records is not None and bool(self.token)  # a full, current cache
            if (rec is None and complete) or (rec is not None and self._is_deleted(rec)):
                continue  # already deleted in iCloud
            if st["deleted_at"] or not st["present"]:
                title = self.store.db.execute("SELECT title FROM notes WHERE id = ?", (st["note_id"],)).fetchone()
                deleted.append({"id": st["note_id"], "title": title["title"] if title else titles.get(name, "")})
        return {
            "edited": [{"id": i, "body": rows[i]["body"]} for i in edited if i in rows and not rows[i]["locked"]],
            "new": [dict(n, body=rows[n["id"]]["body"]) for n in new if n["id"] in rows and rows[n["id"]]["body"].strip()],
            "deleted": deleted,
        }

    def push_prepare(self):
        """Step 1 of Push: pull first, then work out what still has to go out (compared with the fresh state).
        The page shows the list for confirmation and calls push_now with the converted content."""
        if self.busy:
            return
        self.busy = True
        self._set_sync_state("syncing", progress="")

        def run():
            try:
                self._sync()
                candidates = self._on_main(self.sync_candidates)
                payload = json.dumps(candidates)
                GLib.idle_add(lambda: (self.win.eval_js(f"window.N && N.sync && N.sync.onPushReady({payload})"), False)[1])
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

    def push_now(self, outgoing):
        """Push only: send local edits and new notes. Notes changed in iCloud since the last pull are refused."""
        if self.busy:
            return
        self.busy = True
        self._set_sync_state("pushing")

        def run():
            try:
                self._ensure_client()
                stats = {"changed": [], "sent": 0, "created": 0, "skipped": 0}
                self._push_outgoing(outgoing or {}, stats)
                self._save_cache()
                self._log({"push_now": {k: v for k, v in stats.items() if k != "changed"}})
                self._set_sync_state("idle", last=time.time(), changes=stats)
            except NotSignedIn:
                self.client = None
                self._set_sync_state("signin-needed", message="Sign in to iCloud to keep syncing.")
            except urllib.error.URLError:
                self._set_sync_state("offline", message="Can't reach iCloud right now.")
            except Exception as exc:
                traceback.print_exc()
                self._log({"push_error": repr(exc), "trace": traceback.format_exc()})
                self._set_sync_state("error", message=_friendly_error(exc))
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()

    def _push_outgoing(self, outgoing, stats):
        self._ensure_cache()
        stats.update(sent=0, created=0, skipped=0, skippedReasons=[], warnings=[])
        titles = self._on_main(lambda: {r["id"]: r["title"] for r in self.store.db.execute("SELECT id, title FROM notes")})
        for item in outgoing.get("edited") or []:
            title = titles.get(item["id"]) or "Untitled"
            try:
                name, st = self._tracked(item["id"])
                # Pull may have turned it into a conflict (no longer tracked) or it changed again: then don't push.
                if name is None or st["deleted_at"] or (st["updated_at"] or 0) <= (st["modified"] or 0) + 1:
                    continue
                res = self._push_changes(item["id"], item)
                if res.get("ok") and not res.get("unchanged"):
                    stats["sent"] += 1
                    stats["changed"].append(item["id"])
                if res.get("skippedAttachments"):
                    n = res["skippedAttachments"]
                    stats["warnings"].append(f"“{title}”: {n} new {'attachment' if n == 1 else 'attachments'} (image, table or file) "
                                             "stayed on this computer — new attachments can't be created in iCloud yet.")
                if not res.get("ok"):
                    stats["skipped"] += 1
                    stats["skippedReasons"].append(f"“{title}”: {res.get('message') or 'unknown error'}")
            except Exception as exc:
                stats["skipped"] += 1
                stats["skippedReasons"].append(f"“{title}”: {str(exc)[:160]}")
                self._log({"sync_push_skip": item["id"], "error": repr(exc), "trace": traceback.format_exc()})
        deleted = [d["id"] if isinstance(d, dict) else d for d in outgoing.get("deleted") or []]
        if deleted:
            try:
                stats["deleted"] = self._push_trash(deleted).get("trashed", 0)
            except Exception as exc:
                stats["skipped"] += len(deleted)
                stats["skippedReasons"].append(str(exc)[:120])
                self._log({"sync_push_skip": "deleted", "error": repr(exc)})
        for item in outgoing.get("new") or []:
            try:
                res = self._push(item["id"], item, folder=item.get("folder"))
                if res.get("ok"):
                    stats["created"] += 1
            except Exception as exc:
                stats["skipped"] += 1
                stats["skippedReasons"].append(str(exc)[:120])
                self._log({"sync_push_skip": item["id"], "error": repr(exc)})

    def _push_folder(self):
        """The iCloud folder pushed notes go into; created on first use."""
        known = self._on_main(lambda: self.store.mapping(PUSH_SOURCE + "-folder")).get("folder")
        if known:
            return known
        self._ensure_cache()
        for rec in self.records.values():
            if rec.get("recordType") == "Folder" and not rec_int(rec, "Deleted") and \
                    rec_text(rec, "TitleEncrypted") == PUSH_FOLDER_NAME:
                name = rec["recordName"]
                break
        else:
            name = str(uuid.uuid4()).upper()
            ms = int(time.time() * 1000)
            self.client.modify([{"operationType": "create", "record": {
                "recordName": name, "recordType": "Folder",
                "fields": {
                    "TitleEncrypted": {"value": base64.b64encode(PUSH_FOLDER_NAME.encode()).decode(), "type": "ENCRYPTED_BYTES"},
                    "TitleModificationDate": {"value": ms, "type": "TIMESTAMP"},
                    "MinimumSupportedNotesVersion": {"value": 0, "type": "INT64"},
                },
            }}])
            self._on_main(lambda: (self.store.set_mapping(PUSH_SOURCE, "folder:" + name, 0), self.store.commit()))
        self._on_main(lambda: (self.store.set_mapping(PUSH_SOURCE + "-folder", "folder", name), self.store.commit()))
        return name

    def remove_pushed(self):
        """Delete every note (and the folder) this app created in iCloud."""
        if self.busy:
            self._emit_push(None, ok=False, message="iCloud sync is running. Try again in a few seconds.")
            return
        self.busy = True

        def run():
            try:
                self._ensure_client()
                self._ensure_cache()
                pushed = self._on_main(lambda: self.store.mapping(PUSH_SOURCE))
                notes = [n for n in pushed if not n.startswith("folder:")]
                folders = [n[len("folder:"):] for n in pushed if n.startswith("folder:")]
                ops = [{"operationType": "forceDelete", "record": {"recordName": n}} for n in notes + folders]
                if ops:
                    self.client.modify(ops)

                def forget():
                    for n in notes:
                        self.store.forget_import(PUSH_SOURCE, n)
                        self.store.forget_import(SOURCE, n)  # the local copies stay as ordinary local notes
                    for f in folders:
                        self.store.forget_import(PUSH_SOURCE, "folder:" + f)
                        self.store.forget_import(FOLDER_SOURCE, f)
                    self.store.forget_import(PUSH_SOURCE + "-folder", "folder")
                    self.store.commit()

                self._on_main(forget)
                for n in notes + folders:
                    (self.records or {}).pop(n, None)
                self._save_cache()
                self._emit_push(None, ok=True, removed=len(notes))
            except NotSignedIn:
                self.client = None
                self._emit_push(None, ok=False, signin=True, message="Sign in to iCloud first.")
            except Exception as exc:
                traceback.print_exc()
                self._log({"remove_error": repr(exc), "trace": traceback.format_exc()})
                self._emit_push(None, ok=False, message=str(exc))
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()

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
