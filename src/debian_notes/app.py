"""GTK window hosting the WebKit UI, plus the JSON bridge the UI calls into."""

import base64
import html
import json
import mimetypes
import os
import re
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
window.notes-window decoration { border-radius: 10px; }
window.notes-window.maximized decoration { border-radius: 0; }
"""


def data_dir():
    path = os.environ.get("NOTES_DATA_DIR") or os.path.join(GLib.get_user_data_dir(), "debian-notes")
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)  # notes, locked-note keys and the iCloud session live here
    return path


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

    def saveNote(self, note_id, data):
        row = self.store.note_row(note_id)
        if row["locked"]:
            if self.keys is None:
                raise PermissionError("locked")
            return self.store.save_note(note_id, data, crypto.encrypt(self.keys, data.get("body", "")))
        return self.store.save_note(note_id, data)

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
        return self.store.move_note(note_id, folder_id)

    def setPinned(self, note_id, pinned):
        return self.store.set_pinned(note_id, pinned)

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

    def saveAttachment(self, name, data_b64):
        base = os.path.basename(name or "attachment")
        base = re.sub(r"[^\w.\- ]+", "_", base).strip() or "attachment"
        path = os.path.join(self.store.attachments_dir, f"{uuid.uuid4().hex[:12]}-{base}")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data_b64))
        return {"uri": GLib.filename_to_uri(path), "size": os.path.getsize(path)}

    def openExternal(self, uri):
        if not re.match(r"^(https?|mailto|file|tel):", uri, re.I):
            uri = "https://" + uri
        Gtk.show_uri_on_window(self.win, uri, Gtk.get_current_event_time())

    # --------------------------------------------------- import / export

    def exportNote(self, fmt, content, suggested_name):
        if fmt == "pdf":
            self.win.print_page()
            return True
        ext = {"html": "html", "markdown": "md", "text": "txt"}[fmt]
        safe = re.sub(r'[\\/:*?"<>|]+', "", suggested_name or "Note").strip()[:80] or "Note"
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

        return re.sub(r'(<img[^>]*?\ssrc=)"([^"]+)"', repl, html)

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

    def icloudSetSync(self, changes):
        return self._icloud().set_sync_config(changes)

    def icloudSyncState(self):
        if self._icloud_importer is None and not (self.store.get_setting("icloud.sync") or {}).get("enabled"):
            return {"state": "off"}
        imp = self._icloud()
        return dict(imp.sync_state, enabled=imp.sync_config()["enabled"])

    def start_sync_if_enabled(self):
        if (self.store.get_setting("icloud.sync") or {}).get("enabled"):
            imp = self._icloud()
            imp.schedule()
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
            self.set_visual(visual)
            self.set_app_paintable(True)
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
        langs = [l for l in GLib.get_language_names() if re.match(r"^[a-z]{2}(_[A-Z]{2})?$", l)]
        ctx.set_spell_checking_languages((langs or ["en_US"])[:3])

        self.webview.connect("decide-policy", self._on_decide_policy)
        self.webview.connect("context-menu", self._on_context_menu)
        self.webview.connect("button-press-event", self._on_button_press)
        self.webview.connect("button-release-event", self._on_button_release)
        self.webview.connect("motion-notify-event", self._on_motion)
        self.webview.connect("load-changed", self._on_load_changed)
        self.add(self.webview)

        self._watch_system_theme()
        self.apply_theme(self._initial_theme())

        self.connect("delete-event", self._on_delete)
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("window-state-event", self._on_window_state)

        self.webview.load_uri(GLib.filename_to_uri(os.path.join(WEB_DIR, "index.html")))

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

    # ------------------------------------------------------- window dragging

    def _in_drag_area(self, x, y):
        def inside(r):
            return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]
        regions = self.drag_regions or {}
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
        if not os.environ.get("NOTES_SNAPSHOT"):
            GLib.idle_add(self.api.start_sync_if_enabled)
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

    def choose_open_paths(self, title):
        dialog = Gtk.FileChooserNative.new(title, self, Gtk.FileChooserAction.OPEN, "_Import", "_Cancel")
        dialog.set_select_multiple(True)
        flt = Gtk.FileFilter()
        flt.set_name("Text, Markdown and HTML")
        for pattern in ("*.txt", "*.md", "*.markdown", "*.html", "*.htm", "*.TXT", "*.MD", "*.HTML"):
            flt.add_pattern(pattern)
        dialog.add_filter(flt)
        paths = dialog.get_filenames() if dialog.run() == Gtk.ResponseType.ACCEPT else []
        dialog.destroy()
        return paths

    def print_page(self):
        op = WebKit2.PrintOperation.new(self.webview)
        op.run_dialog(self)

    # --------------------------------------------------------------- close

    def _on_delete(self, *_):
        if self._closing:
            return False
        self._closing = True
        self.eval_js("N.beforeQuit ? N.beforeQuit() : window.webkit.messageHandlers.bridge.postMessage("
                     "JSON.stringify({id: null, method: 'quitReady', args: []}))")
        GLib.timeout_add(2500, self.finish_close)
        return True

    def finish_close(self):
        if self._closed:
            return False
        self._closed = True
        maximized = self.is_maximized()
        self.store.set_setting("window.maximized", maximized)
        if not maximized:
            w, h = self.get_size()
            self.store.set_setting("window.width", w)
            self.store.set_setting("window.height", h)
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
        self.window.show_all()
        self.window.present()

    def do_shutdown(self):
        if self.store:
            self.store.close()
        Gtk.Application.do_shutdown(self)


def main():
    return NotesApplication().run(sys.argv)
