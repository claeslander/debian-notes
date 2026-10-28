"""GTK window hosting the WebKit UI, plus the JSON bridge the UI calls into."""

import base64
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import traceback
import uuid

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

from . import __version__, crypto  # noqa: E402
from .store import Store  # noqa: E402
from .welcome import WELCOME_HTML  # noqa: E402

APP_ID = "org.debiannotes.Notes"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DEBUG = bool(os.environ.get("NOTES_DEBUG"))
DRAG_THRESHOLD = 4

GTK_CSS = b"""
.notes-titlebar { min-height: 0; padding: 0; margin: 0; border: none; background: none; box-shadow: none; }
window.notes-window { background: transparent; }
window.notes-window decoration { border-radius: 10px; border: none;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.30), 0 0 0 1px rgba(0, 0, 0, 0.10); }
/* Same shadow geometry when unfocused: GTK sizes the window's shadow margin from it, so a different
   blur/offset would resize and shift the window on every focus change. Only the strength changes. */
window.notes-window decoration:backdrop { box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18), 0 0 0 1px rgba(0, 0, 0, 0.08); }
window.notes-window.maximized decoration { border-radius: 0; }
"""


def data_dir():
    path = os.environ.get("NOTES_DATA_DIR") or os.path.join(GLib.get_user_data_dir(), "debian-notes")
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)  # notes, locked-note keys and the iCloud session live here
    return path


SYSTEM_DICT_DIRS = ["/usr/share/hunspell", "/usr/share/myspell", "/usr/share/myspell/dicts"]
USER_DICT_DIR = os.path.join(GLib.get_user_config_dir(), "enchant", "hunspell")  # Enchant looks here too
DICT_DIRS = SYSTEM_DICT_DIRS + [USER_DICT_DIR]
BUNDLED_DICT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictionaries")
MAX_SPELL_LANGS = 4


def installed_dictionaries(dirs=DICT_DIRS):
    """Language codes (nl, nl_BE, en_US...) with a hunspell dictionary on this machine."""
    found = set()
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            code, ext = os.path.splitext(name)
            if ext == ".dic" and code + ".aff" in names and re.match(r"^[a-z]{2,3}(_[A-Z]{2})?$", code):
                found.add(code)
    return found


def link_bundled_dictionaries(src=BUNDLED_DICT_DIR, dest=USER_DICT_DIR, system_dirs=SYSTEM_DICT_DIRS):
    """Make the dictionaries shipped with the app (Dutch, English) visible to Enchant by linking
    them into its user folder, unless the system already has them. Learned words stay in
    Enchant's own personal word lists, shared with other apps."""
    system = installed_dictionaries(system_dirs)
    for code in sorted(installed_dictionaries([src]) - system):
        for ext in (".aff", ".dic"):
            target, link = os.path.join(src, code + ext), os.path.join(dest, code + ext)
            try:
                if os.path.islink(link) and os.readlink(link) != target:
                    os.remove(link)  # left behind by an older checkout or install
                if not os.path.lexists(link):
                    os.makedirs(dest, exist_ok=True)
                    os.symlink(target, link)
            except OSError:
                traceback.print_exc()


def spelling_languages(locale_langs, installed):
    """The locale's languages first, then every other language with a dictionary (one variant each),
    so mixed-language notes (say English and Dutch) are checked against all of them."""
    def resolve(code):
        if code in installed:
            return code
        base = code.split("_")[0]
        if base in installed:
            return base
        return next((c for c in sorted(installed) if c.startswith(base + "_")), None)

    langs, bases = [], set()
    wanted = [l for l in locale_langs if re.match(r"^[a-z]{2,3}(_[A-Z]{2})?$", l)]
    for code in wanted + sorted(installed):
        hit = resolve(code)
        if hit and hit.split("_")[0] not in bases:
            bases.add(hit.split("_")[0])
            langs.append(hit)
    if not installed:  # unknown dictionary layout: leave it to Enchant
        langs = wanted[:1]
    return (langs or ["en_US"])[:MAX_SPELL_LANGS]


class Api:
    """Every public method here is callable from JavaScript via N.call(name, ...args)."""

    def __init__(self, window, store):
        self.win = window
        self.store = store
        self.keys = None  # session keys while locked notes are open
        self._icloud_importer = None

    # ------------------------------------------------------------ bootstrap

    def bootstrap(self):
        return {
            "version": __version__,
            "folders": self.store.folders(),
            "notes": self.store.notes(),
            "settings": self.store.settings(),
            "defaultFolderId": self.store.default_folder_id(),
            "systemTheme": self.win.system_theme(),
            "lockConfigured": self.store.get_setting("lock.verifier") is not None,
            "lockHint": self.store.get_setting("lock.hint", ""),
            "windowActive": self.win.is_active(),
            "rounded": self.win.rounded,
            "maximized": self.win.is_maximized(),
            "icloudConfigured": self.win.icloud_configured(),
        }

    def log(self, *parts):
        print("[web]", *parts, file=sys.stderr)

    # ---------------------------------------------------------------- notes

    def getNote(self, note_id):
        row = self.store.note_row(note_id)
        if row["locked"]:
            if self.keys is None:
                raise PermissionError("locked")
            return {"body": crypto.decrypt(self.keys, row["locked_payload"])}
        return {"body": row["body"]}

    def createNote(self, folder_id, body=""):
        return self.store.create_note(folder_id, body=body)

    def importNote(self, folder_id, data):
        meta = self.store.create_note(folder_id)
        return self.store.save_note(meta["id"], data)

    def _writable(self, note_id):
        """iCloud notes are read-only here: they can only be read, deleted and copied."""
        if self.store.is_icloud(note_id):
            raise PermissionError("readonly")

    def saveNote(self, note_id, data):
        self._writable(note_id)
        row = self.store.note_row(note_id)
        if row["locked"]:
            if self.keys is None:
                raise PermissionError("locked")
            return self.store.save_note(note_id, data, crypto.encrypt(self.keys, data.get("body", "")))
        return self.store.save_note(note_id, data)

    def noteVersions(self, note_id):
        return self.store.versions(note_id)

    def noteVersion(self, version_id):
        return self.store.version(version_id)

    def snapshotNote(self, note_id, reason):
        """Save the current content as a version (e.g. right before restoring an older one)."""
        self._writable(note_id)
        self.store.snapshot_current(note_id, reason)
        self.store.commit()

    def deleteNote(self, note_id, permanently=False):
        if permanently:
            self.store.delete_note(note_id)
            return None
        return self.store.trash_note(note_id)

    def recoverNote(self, note_id):
        return self.store.recover_note(note_id)

    def emptyTrash(self):
        self.store.empty_trash()

    def moveNote(self, note_id, folder_id):
        self._writable(note_id)
        return self.store.move_note(note_id, folder_id)

    def setPinned(self, note_id, pinned):
        self._writable(note_id)
        return self.store.set_pinned(note_id, pinned)

    def restoreNoteState(self, note_id, folder_id, pinned, deleted_at):
        """Undo/redo of a move, pin or delete: only ever returns a note to a state it was in before."""
        return self.store.restore_note_state(note_id, folder_id, pinned, deleted_at)

    def duplicateNote(self, note_id):
        return self.store.duplicate_note(note_id)

    def search(self, query):
        return self.store.search(query)

    # -------------------------------------------------------------- folders

    def createFolder(self, name, parent_id=None):
        return self.store.create_folder(name, parent_id)

    def renameFolder(self, folder_id, name):
        self.store.rename_folder(folder_id, name)
        return self.store.folders()

    def moveFolder(self, folder_id, parent_id):
        self.store.move_folder(folder_id, parent_id)
        return self.store.folders()

    def deleteFolder(self, folder_id):
        self.store.delete_folder(folder_id)
        return {"folders": self.store.folders(), "notes": self.store.notes()}

    # ------------------------------------------------------------- settings

    def setSetting(self, key, value):
        self.store.set_setting(key, value)

    def setTheme(self, theme):
        self.win.apply_theme(theme)

    # -------------------------------------------------------------- locking

    def setLockPassword(self, password, hint=""):
        if self.store.get_setting("lock.verifier") is not None:
            raise PermissionError("A password is already set")
        if not password:
            raise ValueError("Password can't be empty")
        salt = crypto.new_salt()
        keys = crypto.derive(password, salt)
        self.store.set_setting("lock.salt", salt)
        self.store.set_setting("lock.verifier", crypto.verifier(keys))
        self.store.set_setting("lock.hint", hint)
        self.keys = keys
        return True

    def unlock(self, password):
        salt = self.store.get_setting("lock.salt")
        expected = self.store.get_setting("lock.verifier")
        if salt is None or expected is None:
            return False
        keys = crypto.derive(password, salt)
        if not crypto.check(keys, expected):
            return False
        self.keys = keys
        return True

    def changeLockPassword(self, old, new, hint=""):
        if not self.unlock(old):
            return False
        old_keys = self.keys
        salt = crypto.new_salt()
        new_keys = crypto.derive(new, salt)
        for row in self.store.locked_notes():
            plain = crypto.decrypt(old_keys, row["locked_payload"])
            self.store.update_locked_payload(row["id"], crypto.encrypt(new_keys, plain))
        self.store.set_setting("lock.salt", salt)
        self.store.set_setting("lock.verifier", crypto.verifier(new_keys))
        self.store.set_setting("lock.hint", hint)
        self.store.commit()
        self.keys = new_keys
        return True

    def closeLockedNotes(self):
        self.keys = None

    def lockNote(self, note_id, data):
        self._writable(note_id)
        if self.keys is None:
            raise PermissionError("locked")
        self.store.save_note(note_id, data)
        return self.store.set_locked(note_id, crypto.encrypt(self.keys, data.get("body", "")))

    def removeLock(self, note_id):
        if self.keys is None:
            raise PermissionError("locked")
        row = self.store.note_row(note_id)
        body = crypto.decrypt(self.keys, row["locked_payload"])
        self.store.set_locked(note_id, None)
        return {"body": body, "meta": self.store.note_meta(note_id)}

    # ---------------------------------------------------------- attachments

    def _attachment_path(self, name):
        base = os.path.basename(name or "attachment")
        base = re.sub(r"[^\w.\- ]+", "_", base).strip() or "attachment"
        return os.path.join(self.store.attachments_dir, f"{uuid.uuid4().hex[:12]}-{base}")

    def saveAttachment(self, name, data_b64):
        path = self._attachment_path(name)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data_b64))
        return {"uri": GLib.filename_to_uri(path), "size": os.path.getsize(path)}

    def copyAttachment(self, uri):
        """Copy a file dropped from the file manager (WebKitGTK hands the page only its file:// URI)."""
        src = GLib.filename_from_uri(uri)[0]
        if not os.path.isfile(src):
            raise ValueError(f"not a file: {src}")
        name = os.path.basename(src)
        path = self._attachment_path(name)
        shutil.copyfile(src, path)
        return {"name": name, "type": mimetypes.guess_type(name)[0] or "", "uri": GLib.filename_to_uri(path),
                "size": os.path.getsize(path)}

    def chooseAttachments(self, images_only=False):
        paths = self.win.choose_open_paths("Add Photos" if images_only else "Attach File", "_Add",
                                           "image" if images_only else None)
        out = []
        for path in paths:
            try:
                out.append(self.copyAttachment(GLib.filename_to_uri(path)))
            except (OSError, ValueError) as exc:
                self.log("attach failed", path, exc)
        return out

    def openExternal(self, uri):
        if not re.match(r"^(https?|mailto|file|tel):", uri, re.I):
            uri = "https://" + uri
        Gtk.show_uri_on_window(self.win, uri, Gtk.get_current_event_time())

    # --------------------------------------------------- import / export

    def exportNote(self, fmt, content, suggested_name):
        safe = re.sub(r'[\\/:*?"<>|]+', "", suggested_name or "Note").strip()[:80] or "Note"
        if fmt == "pdf":
            path = self.win.choose_save_path("Export as PDF", f"{safe}.pdf")
            if path:
                self.win.print_page(path)
            return bool(path)
        ext = {"html": "html", "markdown": "md", "text": "txt"}[fmt]
        path = self.win.choose_save_path("Export Note", f"{safe}.{ext}")
        if not path:
            return False
        if fmt == "html":
            content = self._inline_attachments(content)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return True

    def importFiles(self):
        paths = self.win.choose_open_paths("Import to Notes")
        out = []
        for path in paths:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError as exc:
                self.log("import failed", path, exc)
                continue
            out.append({
                "name": os.path.splitext(os.path.basename(path))[0],
                "ext": os.path.splitext(path)[1].lower().lstrip("."),
                "text": text,
                "mtime": os.path.getmtime(path),
            })
        return out

    def _inline_attachments(self, html):
        prefix = GLib.filename_to_uri(self.store.attachments_dir)

        def repl(m):
            uri = m.group(2)
            if not uri.startswith(prefix):
                return m.group(0)
            try:
                path = GLib.filename_from_uri(uri)[0]
                with open(path, "rb") as fh:
                    data = base64.b64encode(fh.read()).decode()
            except (OSError, GLib.Error):
                return m.group(0)
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            return f'{m.group(1)}"data:{mime};base64,{data}"'

        return re.sub(r'(<(?:img|audio)[^>]*?\ssrc=)"([^"]+)"', repl, html)

    # ---------------------------------------------------------------- iCloud

    def _icloud(self):
        if self._icloud_importer is None:
            from .icloud import ICloudImporter
            self._icloud_importer = ICloudImporter(self.win, self.store, self.store.data_dir)
        return self._icloud_importer

    def icloudStart(self):
        self._icloud().start()

    def icloudSignIn(self):
        self._icloud().open_signin()

    def icloudScan(self):
        self._icloud().scan()

    def icloudImport(self, options):
        self._icloud().run_import(options)

    def icloudCancel(self):
        self._icloud().cancel()

    def icloudSignOut(self):
        self._icloud().sign_out()

    def icloudSyncNow(self):
        self._icloud().sync_now()

    def icloudOpenNote(self, note_id):
        self._icloud().open_note_on_web(note_id)

    def icloudNoteUrl(self, note_id):
        return self._icloud().note_url(note_id)

    def showICloudWeb(self, box, url=None):
        self.win.show_icloud_web(box, url)

    def modeChanged(self, mode):
        self.win.mode = mode

    def partsChanged(self, icloud_enabled):
        if not icloud_enabled:
            self.win.hide_icloud_web()

    def freezeICloudWeb(self):
        self.win.freeze_icloud_web()

    def forgetFrozenICloud(self):
        self.win.forget_frozen_icloud()

    def hideICloudWeb(self):
        self.win.hide_icloud_web()

    def icloudSetSync(self, changes):
        return self._icloud().set_sync_config(changes)

    def icloudSyncState(self):
        if self._icloud_importer is None and not (self.store.get_setting("icloud.sync") or {}).get("enabled"):
            return {"state": "off"}
        imp = self._icloud()
        return dict(imp.sync_state, enabled=imp.sync_config()["enabled"])

    def start_sync_if_enabled(self):
        cfg = self.store.get_setting("icloud.sync") or {}
        if cfg.get("enabled"):
            imp = self._icloud()
            imp.schedule()
            if int(cfg.get("interval") or 0) > 0:  # automatic mode also syncs at startup; manual mode waits
                GLib.timeout_add_seconds(5, lambda: (imp.sync_now(quiet=True), False)[1])
        return False

    # --------------------------------------------------------------- window

    def windowControl(self, action):
        if action == "close":
            self.win.close()
        elif action == "minimize":
            self.win.iconify()
        elif action == "maximize":
            self.win.toggle_maximize()
        elif action == "fullscreen":
            self.win.toggle_fullscreen()

    def setDragRegions(self, regions):
        self.win.drag_regions = regions

    def print(self):
        self.win.print_page()

    def quitApp(self):
        self.win.quit_app()

    def setBackground(self, enabled):
        """Background mode: closing hides the window, and Notes starts hidden at login."""
        self.store.set_setting("background", bool(enabled))
        set_autostart(bool(enabled))

    def quitReady(self):
        GLib.idle_add(self.win.finish_close)


EXPOSED = {name for name in dir(Api) if not name.startswith("_")}


class NotesWindow(Gtk.ApplicationWindow):
    def __init__(self, app, store):
        super().__init__(application=app, title="Notes")
        self.store = store
        self.api = Api(self, store)
        self.drag_regions = {"areas": [], "exclude": []}
        self._press = None
        self._closing = False
        self._closed = False
        self._quitting = False
        self._iface_settings = None

        self.set_icon_name("debian-notes")
        if not Gtk.IconTheme.get_default().has_icon("debian-notes"):
            # Running from a source checkout: load the icon straight from data/.
            src_icon = os.path.join(os.path.dirname(__file__), "..", "..", "data", "icons", "debian-notes.svg")
            if os.path.exists(src_icon):
                self.set_icon_from_file(src_icon)
        self.get_style_context().add_class("notes-window")
        # A transparent toplevel lets the page draw its own rounded corners.
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        self.rounded = bool(visual and screen.is_composited())
        if self.rounded:
            # The window background is made transparent through CSS (GTK_CSS). Not app-paintable: that
            # would also stop GTK from drawing the shadow area, which then shows up as a black border.
            self.set_visual(visual)
        self.set_default_size(int(store.get_setting("window.width", 1180)), int(store.get_setting("window.height", 760)))
        if store.get_setting("window.maximized", False):
            self.maximize()

        titlebar = Gtk.Box()
        titlebar.get_style_context().add_class("notes-titlebar")
        titlebar.set_size_request(-1, 0)
        self.set_titlebar(titlebar)

        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("bridge")
        ucm.connect("script-message-received::bridge", self._on_message)

        self.webview = WebKit2.WebView.new_with_user_content_manager(ucm)
        settings = self.webview.get_settings()
        settings.set_allow_file_access_from_file_urls(True)
        settings.set_allow_universal_access_from_file_urls(True)
        settings.set_javascript_can_access_clipboard(True)
        settings.set_enable_developer_extras(DEBUG)
        settings.set_enable_write_console_messages_to_stdout(DEBUG)
        settings.set_enable_back_forward_navigation_gestures(False)

        ctx = self.webview.get_context()
        ctx.set_spell_checking_enabled(True)
        link_bundled_dictionaries()
        ctx.set_spell_checking_languages(spelling_languages(GLib.get_language_names(), installed_dictionaries()))

        self.webview.connect("decide-policy", self._on_decide_policy)
        self.webview.connect("context-menu", self._on_context_menu)
        self.webview.connect("button-press-event", self._on_button_press)
        self.webview.connect("button-release-event", self._on_button_release)
        self.webview.connect("motion-notify-event", self._on_motion)
        self.webview.connect("load-changed", self._on_load_changed)
        # The page fills the window; iCloud Notes on the web can be laid over part of it (the iCloud view).
        self.overlay = Gtk.Overlay()
        self.overlay.add(self.webview)
        self.add(self.overlay)
        self.icloud_view = None
        self.mode = "icloud"
        self.icloud_ready = False
        self.icloud_wanted = False
        self.theme = "light"

        self._watch_system_theme()
        self.apply_theme(self._initial_theme())

        self.connect("delete-event", self._on_delete)
        # Window level, before the focused view: also works while typing in iCloud Notes on the web.
        self.connect("key-press-event", self._on_key_press)
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("window-state-event", self._on_window_state)

        self.webview.load_uri(GLib.filename_to_uri(os.path.join(WEB_DIR, "index.html")))
        GLib.idle_add(self.preload_icloud_web)

    # ---------------------------------------------------------------- bridge

    def _on_message(self, _ucm, result):
        try:
            msg = json.loads(result.get_js_value().to_string())
        except Exception:  # malformed message from the page
            traceback.print_exc()
            return
        call_id, method, args = msg.get("id"), msg.get("method"), msg.get("args") or []
        if method not in EXPOSED:
            self._reply(call_id, False, f"Unknown method {method}")
            return
        try:
            value = getattr(self.api, method)(*args)
        except PermissionError as exc:
            self._reply(call_id, False, str(exc) or "locked")
            return
        except Exception as exc:
            traceback.print_exc()
            self._reply(call_id, False, str(exc))
            return
        self._reply(call_id, True, value)

    def _reply(self, call_id, ok, value):
        if call_id is None:
            return
        self.eval_js(f"window.N && N._resolve({json.dumps(call_id)}, {'true' if ok else 'false'}, {json.dumps(value)})")

    # ------------------------------------------------- iCloud on the web

    # The iCloud view starts loading as soon as the window opens, and only appears once iCloud Notes has
    # drawn the notes: until then the page shows its own calm "Loading" screen instead of a page being
    # built up piece by piece.
    # Ready: the notes app frame has drawn its notes (not just its frame with Apple's own "Loading…") and
    # stopped changing. Another page (Apple's sign-in, an error) is shown a few seconds after it loaded.
    ICLOUD_READY_JS = """(() => { const f = document.querySelector('iframe');
      if (!f) return '';
      const d = f.contentDocument; const t = (d && d.body && d.body.innerText || '').trim();
      if (!d || d.readyState !== 'complete' || t.length < 150 || /Loading/.test(t)) return '';
      return 'notes:' + d.getElementsByTagName('*').length + ':' + t.length; })()"""

    def _icloud_view(self, url=None):
        from .icloud import USER_AGENT
        if self.icloud_view is not None:
            if url:
                self.icloud_view.load_uri(url)
            return self.icloud_view
        view = WebKit2.WebView.new_with_context(self.api._icloud().session.context)
        view.get_settings().set_user_agent(USER_AGENT)
        # Pop-ups (Apple's sign-in, share sheets) stay in this view.
        view.connect("create", lambda v, action: (v.load_request(action.get_request()), None)[1])
        view.connect("load-changed", self._on_icloud_load)
        view.connect("load-failed", self._on_icloud_load_failed)
        view.set_halign(Gtk.Align.FILL)
        view.set_valign(Gtk.Align.FILL)
        view.set_no_show_all(True)
        self._paint_icloud_view(view)
        self.overlay.add_overlay(view)
        self.icloud_view = view
        self.icloud_ready = False
        self.icloud_wanted = False
        self._icloud_last, self._icloud_settle = "", 0
        view.load_uri(url or "https://www.icloud.com/notes/")
        GLib.timeout_add(150, self._poll_icloud_ready)
        return view

    def _paint_icloud_view(self, view):
        rgba = Gdk.RGBA()
        rgba.parse("#1e1e1e" if self.theme == "dark" else "#ffffff")
        view.set_background_color(rgba)  # no white flash in dark mode

    def _on_icloud_load(self, _view, event):
        if event == WebKit2.LoadEvent.FINISHED and not self.icloud_ready:
            # Never keep it hidden for long, whatever the page does.
            GLib.timeout_add_seconds(9, lambda: (self._icloud_is_ready(), False)[1])

    def _on_icloud_load_failed(self, _view, _event, uri, error):
        """No network yet (the app started at login before Wi-Fi was up) or it dropped: load the page again as
        soon as the network is back, and every few seconds in case that is never reported."""
        if error.matches(WebKit2.NetworkError.quark(), WebKit2.NetworkError.CANCELLED) or \
                error.matches(WebKit2.PolicyError.quark(), WebKit2.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
            return False  # replaced by another load, not a failure
        self._icloud_failed_uri = uri
        if not getattr(self, "_icloud_retry_hooked", False):
            self._icloud_retry_hooked = True
            Gio.NetworkMonitor.get_default().connect(
                "network-changed", lambda _m, available: available and self._retry_icloud())
        if not getattr(self, "_icloud_retry_timer", 0):
            self._icloud_retry_timer = GLib.timeout_add_seconds(5, self._on_icloud_retry_timer)
        return False  # WebKit shows its error page meanwhile

    def _on_icloud_retry_timer(self):
        self._icloud_retry_timer = 0
        return self._retry_icloud()

    def _retry_icloud(self):
        uri, self._icloud_failed_uri = getattr(self, "_icloud_failed_uri", None), None
        if getattr(self, "_icloud_retry_timer", 0):
            GLib.source_remove(self._icloud_retry_timer)
            self._icloud_retry_timer = 0
        if uri and self.icloud_view is not None:
            self.icloud_view.load_uri(uri)
        return False

    def _poll_icloud_ready(self):
        if self.icloud_ready or self.icloud_view is None:
            return False

        def got(view, res):
            try:
                state = view.evaluate_javascript_finish(res).to_string()
            except GLib.Error:
                return
            if state:
                if not self.store.get_setting("icloud.configured"):
                    self.store.set_setting("icloud.configured", True)  # signed in: the app opens on iCloud from now on
                # The notes are there; wait until iCloud stops building the page (list, note, tables): its
                # requests never settle (it keeps polling for changes), so look at what is drawn.
                self._icloud_settle = self._icloud_settle + 1 if state == self._icloud_last else 0
                self._icloud_last = state
                if self._icloud_settle >= 10:
                    self._icloud_is_ready()

        self.icloud_view.evaluate_javascript(self.ICLOUD_READY_JS, -1, None, None, None, got)
        return True

    def _icloud_is_ready(self):
        if self.icloud_ready or self.icloud_view is None:
            return
        self.icloud_ready = True
        self.icloud_view.set_opacity(1)

    def icloud_configured(self):
        """Whether iCloud has been set up: its notes were shown once, or a signed-in session is already saved.
        Until then the app opens on Local."""
        if self.store.get_setting("icloud.configured"):
            return True
        try:
            with open(os.path.join(self.store.data_dir, "icloud-web", "session.json"), encoding="utf-8") as fh:
                return "X-APPLE-WEBAUTH-TOKEN" in json.load(fh)
        except (OSError, ValueError, TypeError):
            return False

    def preload_icloud_web(self):
        """Start loading iCloud Notes on the web while the app itself starts: when it opens on iCloud, or to
        have it ready in background mode."""
        if self.store.get_setting("icloudEnabled", True) is False:
            return False
        if self.icloud_configured() and (self.store.get_setting("lastMode") != "local" or self.store.get_setting("background")):
            self._icloud_view()
        return False

    def show_icloud_web(self, box, url=None):
        """Lay iCloud Notes on the web over the page area in `box` (CSS pixels from each window edge)."""
        view = self._icloud_view(url)
        scale = self._css_scale = self.css_scale(box.get("pageWidth"))  # box is in page pixels
        view.set_margin_start(max(0, round(box.get("left", 0) * scale)))
        view.set_margin_top(max(0, round(box.get("top", 0) * scale)))
        view.set_margin_end(max(0, round(box.get("right", 0) * scale)))
        view.set_margin_bottom(max(0, round(box.get("bottom", 0) * scale)))
        self.icloud_wanted = True
        # While loading it is shown fully transparent: iCloud only draws some parts (tables, images) on a
        # visible page, and it appears all at once when done.
        view.set_opacity(1 if self.icloud_ready else 0)
        view.show()

    def _on_key_press(self, _widget, event):
        """Ctrl+Left / Ctrl+Right: switch between iCloud (left of the switch) and Local (right).
        Ctrl+Tab (with or without Shift): switch to the other one. Ctrl+Q: quit. Ctrl+,: settings."""
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl = Gdk.ModifierType.CONTROL_MASK
        if mods == ctrl and event.keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.quit_app()
            return True
        if mods == ctrl and event.keyval == Gdk.KEY_comma:
            self.eval_js("window.N && N.app && N.app.openSettings()")  # also while typing in iCloud
            return True
        if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab) and mods in (ctrl, ctrl | Gdk.ModifierType.SHIFT_MASK):
            mode = "local" if self.mode == "icloud" else "icloud"
            self.eval_js(f"window.N && N.app && N.app.setMode('{mode}')")
            return True
        if mods != ctrl or event.keyval not in (Gdk.KEY_Left, Gdk.KEY_Right):
            return False
        mode = "icloud" if event.keyval == Gdk.KEY_Left else "local"
        if mode == self.mode:
            return False  # already there: the key does what it normally does (jump a word)
        self.eval_js(f"window.N && N.app && N.app.setMode('{mode}')")
        return True

    def _frozen_path(self):
        # In the user's runtime folder: in memory, private to the user, gone at logout.
        return os.path.join(GLib.get_user_runtime_dir() or GLib.get_tmp_dir(), "debian-notes-icloud.png")

    def freeze_icloud_web(self):
        """A sheet or menu of the page opens over iCloud: put a picture of iCloud in its place first, so it stays
        in view (dimmed) instead of leaving an empty grey area while the live view steps aside."""
        view = self.icloud_view
        if view is None or not view.get_visible() or not self.icloud_ready:
            self.eval_js("N.app.icloudFrozen(null)")  # nothing to picture: the page just hides it
            return
        path = self._frozen_path()

        def done(v, res):
            try:
                surface = v.get_snapshot_finish(res)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    surface.write_to_png(fh)
                # The page shows the picture exactly where the live view is, then asks to hide it (hideICloudWeb).
                a = v.get_allocation()
                x, y = v.translate_coordinates(self.webview, 0, 0) or (a.x, a.y)
                s = getattr(self, "_css_scale", 1.0)  # screen pixels to page pixels
                rect = {"left": x / s, "top": y / s, "width": a.width / s, "height": a.height / s}
                self.eval_js(f"N.app.icloudFrozen({json.dumps(GLib.filename_to_uri(path))}, {json.dumps(rect)})")
            except Exception:  # never leave the page waiting: then it just hides the live view
                traceback.print_exc()
                self.eval_js("N.app.icloudFrozen(null)")

        view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, done)

    def forget_frozen_icloud(self):
        try:
            os.remove(self._frozen_path())
        except OSError:
            pass

    def hide_icloud_web(self):
        """Returns whether it was showing."""
        if self.icloud_view is None:
            return False
        was = self.icloud_wanted
        self.icloud_wanted = False
        self.icloud_view.hide()
        return was

    def eval_js(self, script):
        if hasattr(self.webview, "evaluate_javascript"):
            self.webview.evaluate_javascript(script, -1, None, None, None, None, None)
        else:
            self.webview.run_javascript(script, None, None, None)

    # --------------------------------------------------------------- theming

    def _watch_system_theme(self):
        source = Gio.SettingsSchemaSource.get_default()
        schema = source.lookup("org.gnome.desktop.interface", True) if source else None
        if schema and schema.has_key("color-scheme"):
            self._iface_settings = Gio.Settings.new("org.gnome.desktop.interface")
            self._iface_settings.connect("changed::color-scheme", self._on_system_theme_changed)

    def system_theme(self):
        forced = os.environ.get("NOTES_THEME")
        if forced in ("light", "dark"):
            return forced
        if self._iface_settings is not None:
            return "dark" if self._iface_settings.get_string("color-scheme") == "prefer-dark" else "light"
        name = Gtk.Settings.get_default().get_property("gtk-theme-name") or ""
        return "dark" if "dark" in name.lower() else "light"

    def _initial_theme(self):
        pref = self.store.get_setting("appearance", "system")
        return pref if pref in ("light", "dark") else self.system_theme()

    def _on_system_theme_changed(self, *_):
        self.eval_js(f"window.N && N.onSystemTheme && N.onSystemTheme({json.dumps(self.system_theme())})")

    def apply_theme(self, theme):
        dark = theme == "dark"
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark)
        rgba = Gdk.RGBA()
        rgba.parse("rgba(0,0,0,0)" if self.rounded else "#1e1e1e" if dark else "#ffffff")
        self.webview.set_background_color(rgba)
        self.theme = theme
        if self.icloud_view is not None:
            self._paint_icloud_view(self.icloud_view)

    # ------------------------------------------------------- window dragging

    def css_scale(self, page_width):
        """Screen pixels per page pixel. Not always 1: WebKit follows GNOME's text scaling (e.g. 1.04), so the
        page's coordinates must be converted before comparing them with GTK's."""
        alloc = self.webview.get_allocated_width()
        return alloc / page_width if page_width and alloc > 1 else 1.0

    def _in_drag_area(self, x, y):
        regions = self.drag_regions or {}
        scale = self.css_scale(regions.get("width"))
        x, y = x / scale, y / scale  # the regions are in page pixels

        def inside(r):
            return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]
        return any(inside(r) for r in regions.get("areas", [])) and not any(
            inside(r) for r in regions.get("exclude", [])
        )

    def _on_button_press(self, _widget, event):
        if event.button != 1 or not self._in_drag_area(event.x, event.y):
            return False
        self.eval_js("window.N && N.ui && N.ui.closeTransient && N.ui.closeTransient()")
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self._press = None
            self.toggle_maximize()
            return True
        if event.type == Gdk.EventType.BUTTON_PRESS:
            self._press = (event.x, event.y)
            return True
        return True

    def _on_motion(self, _widget, event):
        if self._press is None:
            return False
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            self._press = None
            return False
        dx, dy = event.x - self._press[0], event.y - self._press[1]
        if dx * dx + dy * dy >= DRAG_THRESHOLD * DRAG_THRESHOLD:
            self._press = None
            self.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
        return True

    def _on_button_release(self, _widget, event):
        if self._press is not None:
            self._press = None
            return True
        return False

    def toggle_maximize(self):
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def toggle_fullscreen(self):
        if self.get_window() and self.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_window_state(self, _widget, event):
        maximized = bool(event.new_window_state & (Gdk.WindowState.MAXIMIZED | Gdk.WindowState.FULLSCREEN))
        ctx = self.get_style_context()
        (ctx.add_class if maximized else ctx.remove_class)("maximized")
        self.eval_js(f"window.N && N.onWindowState && N.onWindowState({{maximized: {'true' if maximized else 'false'}}})")
        return False

    def _on_active_changed(self, *_):
        self.eval_js(f"window.N && N.onWindowActive && N.onWindowActive({'true' if self.is_active() else 'false'})")

    # ----------------------------------------------------------- web policy

    def _on_decide_policy(self, _view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(GLib.filename_to_uri(WEB_DIR)) and kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
                return False
            decision.ignore()
            if not uri.startswith("about:"):
                try:
                    Gtk.show_uri_on_window(self, uri, Gtk.get_current_event_time())
                except GLib.Error as exc:
                    print("Could not open", uri, exc, file=sys.stderr)
            return True
        return False

    def _on_context_menu(self, _view, menu, _event, _hit):
        unwanted = {
            WebKit2.ContextMenuAction.RELOAD, WebKit2.ContextMenuAction.GO_BACK,
            WebKit2.ContextMenuAction.GO_FORWARD, WebKit2.ContextMenuAction.STOP,
            WebKit2.ContextMenuAction.OPEN_LINK, WebKit2.ContextMenuAction.OPEN_LINK_IN_NEW_WINDOW,
            WebKit2.ContextMenuAction.DOWNLOAD_LINK_TO_DISK, WebKit2.ContextMenuAction.OPEN_IMAGE_IN_NEW_WINDOW,
            WebKit2.ContextMenuAction.DOWNLOAD_IMAGE_TO_DISK, WebKit2.ContextMenuAction.OPEN_FRAME_IN_NEW_WINDOW,
            WebKit2.ContextMenuAction.OPEN_VIDEO_IN_NEW_WINDOW, WebKit2.ContextMenuAction.OPEN_AUDIO_IN_NEW_WINDOW,
        }
        if not DEBUG:
            unwanted.add(WebKit2.ContextMenuAction.INSPECT_ELEMENT)
        for item in list(menu.get_items()):
            if item.get_stock_action() in unwanted:
                menu.remove(item)
        # Drop leading/trailing/duplicate separators left behind.
        items = menu.get_items()
        prev_sep = True
        for item in list(items):
            sep = item.is_separator()
            if sep and prev_sep:
                menu.remove(item)
            prev_sep = sep
        items = menu.get_items()
        if items and items[-1].is_separator():
            menu.remove(items[-1])
        return menu.get_n_items() == 0

    def _on_load_changed(self, _view, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        snapshot = os.environ.get("NOTES_SNAPSHOT")
        if snapshot:
            script = os.environ.get("NOTES_EVAL")
            if script:
                GLib.timeout_add(1200, lambda: (self.eval_js(script), False)[1])
            GLib.timeout_add(int(os.environ.get("NOTES_SNAPSHOT_DELAY", "2500")), self._take_snapshot, snapshot)

    def _take_snapshot(self, path):
        def done(view, res):
            try:
                view.get_snapshot_finish(res).write_to_png(path)
                print("snapshot written to", path)
            except GLib.Error as exc:
                print("snapshot failed:", exc, file=sys.stderr)
            if self.icloud_view is not None and self.icloud_view.get_visible():
                # The iCloud view is a separate widget over the page: write it next to the page snapshot.
                v = self.icloud_view
                v.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None,
                               lambda w, r: (w.get_snapshot_finish(r).write_to_png(path[:-4] + "-icloud.png"),
                                             self.finish_close()))
                return
            self.finish_close()

        self.webview.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, done)
        return False

    # -------------------------------------------------------------- dialogs

    def choose_save_path(self, title, name):
        dialog = Gtk.FileChooserNative.new(title, self, Gtk.FileChooserAction.SAVE, "_Save", "_Cancel")
        dialog.set_current_name(name)
        dialog.set_do_overwrite_confirmation(True)
        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
        if docs:
            dialog.set_current_folder(docs)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        return path

    def choose_open_paths(self, title, accept="_Import", kind="text"):
        dialog = Gtk.FileChooserNative.new(title, self, Gtk.FileChooserAction.OPEN, accept, "_Cancel")
        dialog.set_select_multiple(True)
        flt = Gtk.FileFilter()
        if kind == "text":
            flt.set_name("Text, Markdown and HTML")
            for pattern in ("*.txt", "*.md", "*.markdown", "*.html", "*.htm", "*.TXT", "*.MD", "*.HTML"):
                flt.add_pattern(pattern)
        elif kind == "image":
            flt.set_name("Images")
            flt.add_mime_type("image/*")
        if kind:
            dialog.add_filter(flt)
        paths = dialog.get_filenames() if dialog.run() == Gtk.ResponseType.ACCEPT else []
        dialog.destroy()
        return paths

    def print_page(self, pdf_path=None):
        """Print the open note (Ctrl+P), or write it straight to a PDF file. The page's print style lays the
        note out on A4 in the light look; the margins come from here."""
        setup = Gtk.PageSetup()
        setup.set_paper_size(Gtk.PaperSize.new(Gtk.PAPER_NAME_A4))
        for side in ("top", "bottom", "left", "right"):
            getattr(setup, f"set_{side}_margin")(20, Gtk.Unit.MM)
        op = WebKit2.PrintOperation.new(self.webview)
        op.set_page_setup(setup)
        if not pdf_path:
            op.run_dialog(self)
            return
        settings = Gtk.PrintSettings()
        settings.set_printer("Print to File")
        settings.set(Gtk.PRINT_SETTINGS_OUTPUT_FILE_FORMAT, "pdf")
        settings.set(Gtk.PRINT_SETTINGS_OUTPUT_URI, GLib.filename_to_uri(pdf_path))
        op.set_print_settings(settings)
        op.connect("finished", lambda *_: self.eval_js("N.ui && N.ui.toast('Exported as PDF')"))
        op.connect("failed", lambda _op, err: self.eval_js(f"N.ui && N.ui.alert('Couldn’t export the PDF', {json.dumps(err.message)})"))
        op.print_()

    # --------------------------------------------------------------- close

    def _on_delete(self, *_):
        if self._closing:
            return False
        if self.store.get_setting("background") and not self._quitting:
            # Background mode: only hide, so iCloud Notes stays loaded and the app opens instantly.
            self.eval_js("N.beforeHide && N.beforeHide()")
            self._save_geometry()
            self.hide()
            return True
        self._closing = True
        self.eval_js("N.beforeQuit ? N.beforeQuit() : window.webkit.messageHandlers.bridge.postMessage("
                     "JSON.stringify({id: null, method: 'quitReady', args: []}))")
        GLib.timeout_add(2500, self.finish_close)
        return True

    def quit_app(self):
        """Really quit, also in background mode (Ctrl+Q, Quit Notes). The open note is saved first."""
        self._quitting = True
        self.close()

    def _save_geometry(self):
        maximized = self.is_maximized()
        self.store.set_setting("window.maximized", maximized)
        if not maximized:
            w, h = self.get_size()
            self.store.set_setting("window.width", w)
            self.store.set_setting("window.height", h)

    def finish_close(self):
        if self._closed:
            return False
        self._closed = True
        self.forget_frozen_icloud()
        self._save_geometry()
        self.destroy()
        return False


class NotesApplication(Gtk.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.FLAGS_NONE
        if os.environ.get("NOTES_DATA_DIR"):
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.window = None
        self.store = None
        self.start_hidden = False  # --background: started at login, window stays hidden until opened

    def do_startup(self):
        Gtk.Application.do_startup(self)
        GLib.set_application_name("Notes")
        GLib.set_prgname("debian-notes")
        provider = Gtk.CssProvider()
        provider.load_from_data(GTK_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.store = Store(data_dir())
        if self.store.first_run:
            text = re.sub(r"</(h1|h2|h3|div|li|tr|blockquote|ul|ol)>", "\n", WELCOME_HTML)
            text = html.unescape(re.sub(r"<[^>]+>", "", text))
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            self.store.create_note(
                self.store.default_folder_id(), body=WELCOME_HTML, title=lines[0],
                snippet=" ".join(lines[1:])[:240], plain="\n".join(lines), tags=["welcome"],
            )

    def do_activate(self):
        if self.window is None:
            self.window = NotesWindow(self, self.store)
            self.window.connect("destroy", lambda *_: setattr(self, "window", None))
            if self.start_hidden:
                self.start_hidden = False
                return  # the (hidden) window keeps the app running until it is opened
        self.window.show_all()
        self.window.present()

    def do_shutdown(self):
        if self.store:
            self.store.close()
        Gtk.Application.do_shutdown(self)


AUTOSTART = os.path.join(GLib.get_user_config_dir(), "autostart", APP_ID + ".desktop")


def set_autostart(enabled):
    """Start Notes hidden at login (background mode), with the launcher this copy was started from."""
    if not enabled:
        try:
            os.remove(AUTOSTART)
        except FileNotFoundError:
            pass
        return
    launcher = os.path.realpath(sys.argv[0]) if os.path.basename(sys.argv[0]) == "debian-notes" else "debian-notes"
    os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
    with open(AUTOSTART, "w", encoding="utf-8") as fh:
        fh.write("[Desktop Entry]\nType=Application\nName=Notes (background)\n"
                 f"Exec={launcher} --background\nIcon=debian-notes\nNoDisplay=true\n"
                 "X-GNOME-Autostart-enabled=true\n")


def main():
    app = NotesApplication()
    argv = [a for a in sys.argv if a != "--background"]
    app.start_hidden = len(argv) != len(sys.argv)
    return app.run(argv)
