"""X-Downloader GUI: a dark-themed desktop application built with CustomTkinter."""
import logging
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import threading
import tkinter as tk

import customtkinter as ctk

from . import __version__
from .config import Config
from .core import DownloadThread
from .env import BIN_SUFFIX, EnvManager
from .fsutil import folder_problem, remove_stale_temp_files
from .i18n import LANGUAGE_LABELS, LANGUAGE_OPTIONS, format_log_entry, t
from .sites import SITES, extract_urls
from .system import (
    LockManager, ProcessManager, QueueManager, atomic_write_lines,
    host_env, merge_unique, read_lines, setup_logger,
)


# --- Theme & widget helpers (change the look in one place) ---
class Theme:
    BG = "#000000"
    BOX_BG = "#16181C"
    TEXT = "#FFFFFF"
    BORDER = "#2F3336"
    BTN_BG = "#16181C"
    BTN_HOVER = "#272C30"
    ACCENT = "#FFFFFF"
    ACCENT_HOVER = "#D9D9D9"
    CORNER_RADIUS = 8

    FONT_CANDIDATES = (
        "Noto Sans CJK TC",
        "Noto Sans CJK SC",
        "Noto Sans CJK JP",
        "Source Han Sans TW",
        "Source Han Sans TC",
        "WenQuanYi Micro Hei",
        "WenQuanYi Zen Hei",
        "Droid Sans Fallback",
    )
    family = "TkDefaultFont"

    @classmethod
    def resolve_fonts(cls):
        try:
            import tkinter.font as tkfont
            available = set(tkfont.families())
        except Exception:
            return
        cls.family = next((f for f in cls.FONT_CANDIDATES if f in available), "TkDefaultFont")

    @classmethod
    def font(cls, size: int = 13, bold: bool = False) -> tuple:
        return (cls.family, size, "bold") if bold else (cls.family, size)


def outline_button(parent, text, command, width, height, size=13) -> ctk.CTkButton:
    return ctk.CTkButton(
        parent, text=text, width=width, height=height, font=Theme.font(size),
        fg_color=Theme.BTN_BG, hover_color=Theme.BTN_HOVER,
        border_color=Theme.BORDER, border_width=1, text_color=Theme.TEXT,
        corner_radius=Theme.CORNER_RADIUS,
        command=command,
    )


def option_menu(parent, values: list[str], current: str, command, width: int = 90) -> ctk.CTkOptionMenu:
    menu = ctk.CTkOptionMenu(
        parent, values=values, width=width, height=30,
        font=Theme.font(12), fg_color=Theme.BTN_BG, button_color=Theme.BORDER,
        button_hover_color=Theme.BTN_HOVER, dropdown_fg_color=Theme.BOX_BG,
        corner_radius=Theme.CORNER_RADIUS,
        command=command,
    )
    menu.set(current)
    return menu


def text_box(parent, **kwargs) -> ctk.CTkTextbox:
    return ctk.CTkTextbox(
        parent, font=Theme.font(),
        fg_color=Theme.BOX_BG, text_color=Theme.TEXT,
        border_color=Theme.BORDER, border_width=1,
        corner_radius=Theme.CORNER_RADIUS,
        activate_scrollbars=True, **kwargs,
    )


def _alive(widget) -> bool:
    """True if *widget* exists and was not destroyed (None counts as gone)."""
    return widget is not None and widget.winfo_exists()


# --- Log Handler for UI ---
class GUILogHandler(logging.Handler):
    """Intercepts logger records and colors them by level.

    Tk is not thread-safe: emit() may be called from any thread, so records are only
    queued here and written to the textboxes by drain(), which runs in the Tk main loop.
    """
    MAX_LINES = 1000
    KEEP_LINES = 900
    LEVEL_COLORS = {
        "INFO": "#FFFFFF",
        "WARNING": "#FFD6A5",
        "ERROR": "#FFADAD",
        "CRITICAL": "#FFADAD",
        "DEBUG": "#9CA3AF",
    }

    def __init__(self, textbox: ctk.CTkTextbox):
        super().__init__()
        self.textboxes = [textbox]
        self._records: queue.SimpleQueue = queue.SimpleQueue()
        self._history: list[tuple] = []
        self._setup_tags(textbox)

    def _setup_tags(self, textbox: ctk.CTkTextbox):
        for level, color in self.LEVEL_COLORS.items():
            textbox.tag_config(level, foreground=color)

    def add_textbox(self, textbox: ctk.CTkTextbox):
        self._setup_tags(textbox)
        self.textboxes.append(textbox)
        self._write_items(textbox, self._render(self._history), replace=True)

    def remove_textbox(self, textbox: ctk.CTkTextbox):
        if textbox in self.textboxes:
            self.textboxes.remove(textbox)

    def emit(self, record):
        try:
            exc_text = record.exc_text
            if not exc_text and record.exc_info:
                fmt = self.formatter or logging.Formatter()
                exc_text = fmt.formatException(record.exc_info)
            self._records.put((record.msg, record.args, exc_text, record.levelname))
        except Exception:
            self.handleError(record)

    @staticmethod
    def _render(records) -> list[tuple[str, str]]:
        """(text, level) lines for raw (msg, args, exc_text, level) records, in the active language."""
        return [(format_log_entry(msg, args, exc_text), level) for msg, args, exc_text, level in records]

    def _write_items(self, tb: ctk.CTkTextbox, items: list[tuple[str, str]], replace: bool = False):
        try:
            if not tb.winfo_exists():
                self.remove_textbox(tb)
                return
            tb.configure(state="normal")
            if replace:
                tb.delete("1.0", "end")
            for msg, level in items:
                tb.insert("end", msg + "\n", level)

            # Memory leak prevention: limit max lines (keep latest KEEP_LINES lines)
            # .index("end-1c") returns a string like "1005.0"
            current_lines = int(float(tb.index("end-1c")))
            if current_lines > self.MAX_LINES:
                tb.delete("1.0", f"{current_lines - self.KEEP_LINES}.0")

            tb.configure(state="disabled")
            tb.see("end")
        except tk.TclError:
            self.remove_textbox(tb)

    def drain(self, max_items: int = 500):
        """Must be called from the Tk main thread."""
        raw_items = []
        while len(raw_items) < max_items:
            try:
                raw_items.append(self._records.get_nowait())
            except queue.Empty:
                break
        if not raw_items:
            return

        self._history.extend(raw_items)
        if len(self._history) > self.MAX_LINES:
            self._history = self._history[-self.KEEP_LINES:]

        formatted = self._render(raw_items)
        for tb in list(self.textboxes):
            self._write_items(tb, formatted, replace=False)

    def refresh_language(self):
        """Re-renders all stored log records in the active language."""
        self.drain()
        formatted = self._render(self._history)
        for tb in list(self.textboxes):
            self._write_items(tb, formatted, replace=True)

    def clear(self):
        """Clears the log history and all attached textboxes."""
        self.drain()
        self._history.clear()
        for tb in list(self.textboxes):
            self._write_items(tb, [], replace=True)


# --- Dialog base ---
class _Dialog(ctk.CTkToplevel):
    """Dark Toplevel centred on its parent. Modal dialogs grab the input as soon as the window is mapped."""
    _W, _H = 400, 200
    _MIN_SIZE: tuple[int, int] | None = None     # None: fixed size
    _GROWS = False                               # True: the content sets the height (never less than _H), width is fixed
    _MODAL = True

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self._grab_id = None
        self.title(title)
        if self._GROWS:
            # No geometry() call: the window takes the size its content asks for, but not less than _W x _H.
            self.minsize(self._W, self._H)
            self.resizable(False, False)
        else:
            self.geometry(f"{self._W}x{self._H}")
            if self._MIN_SIZE:
                self.minsize(*self._MIN_SIZE)
            else:
                self.minsize(self._W, self._H)
                self.maxsize(self._W, self._H)
        self.configure(fg_color=Theme.BG)

        # A transient child of an unmapped parent is never shown by some window managers.
        if parent.winfo_viewable():
            self.transient(parent)

        parent.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self._W) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self._H) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        if self._MODAL:
            # grab_set() raises "grab failed: window not viewable" if called before the
            # window is mapped (common on X11), so retry until it succeeds.
            self._grab_id = self.after(0, self._try_grab)

    def _try_grab(self, attempts: int = 40):
        self._grab_id = None
        try:
            if not self.winfo_exists():
                return
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            if attempts > 0:
                try:
                    self._grab_id = self.after(50, self._try_grab, attempts - 1)
                except tk.TclError:
                    pass   # The application was destroyed in the meantime.

    def destroy(self):
        if getattr(self, "_grab_id", None) is not None:
            try:
                self.after_cancel(self._grab_id)
            except tk.TclError:
                pass
            self._grab_id = None
        super().destroy()


# --- Custom MessageBox ---
class CustomMessageBox(_Dialog):
    _GROWS = True       # a long message (a folder path in an error) needs more than 200 px

    def __init__(self, parent, title, message, mode="info"):
        super().__init__(parent, title)
        self.result = None

        ctk.CTkLabel(
            self, text=message, font=Theme.font(), text_color=Theme.TEXT,
            wraplength=350, justify="center"
        ).pack(expand=True, fill="both", padx=20, pady=(30, 10))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(0, 20))

        if mode == "askyesno":
            outline_button(btn_frame, t("btn_yes"), self._on_yes, 80, 30).pack(side="left", padx=10)
            outline_button(btn_frame, t("btn_no"), self._on_no, 80, 30).pack(side="right", padx=10)
        else:
            outline_button(btn_frame, t("btn_ok"), self._on_yes, 80, 30).pack()

        self.protocol("WM_DELETE_WINDOW", self._on_no)

    def _on_yes(self):
        self.result = True
        self.destroy()

    def _on_no(self):
        self.result = False
        self.destroy()

    def show(self):
        self.wait_window()
        return self.result


# --- Custom Directory Chooser Dialog ---
class DirectoryChooserDialog(_Dialog):
    _W, _H = 680, 480
    _MIN_SIZE = (580, 380)

    def __init__(self, parent, initialdir: str = "", title: str = ""):
        super().__init__(parent, title or t("dlg_select_dir"))
        self.result = ""
        initial = Config.resolve_path(initialdir) if initialdir else Config.BASE_PATH
        self._current_path = initial if os.path.isdir(initial) else Config.BASE_PATH
        self._selected_path = self._current_path
        self._folder_buttons: list[ctk.CTkButton] = []

        self._build_layout()
        self._navigate_to(self._current_path)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build_layout(self):
        # 1. Top bar: Up, Path entry, Go
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.pack(fill="x", padx=16, pady=(16, 8))

        outline_button(top_bar, "▲ " + t("btn_up"), self._on_up, width=75, height=32).pack(side="left", padx=(0, 6))

        self._path_entry = ctk.CTkEntry(
            top_bar, font=Theme.font(12), fg_color=Theme.BOX_BG,
            text_color=Theme.TEXT, border_color=Theme.BORDER, border_width=1,
            corner_radius=Theme.CORNER_RADIUS, height=32,
        )
        self._path_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._path_entry.bind("<Return>", lambda e: self._on_path_entered())

        outline_button(top_bar, "➔", self._on_path_entered, width=36, height=32).pack(side="left")

        # 2. Main area: Quick bookmarks sidebar + Directory list
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=16, pady=4)

        sidebar = ctk.CTkFrame(
            main_frame, width=120, fg_color="#0D0E10",
            border_color=Theme.BORDER, border_width=1,
            corner_radius=Theme.CORNER_RADIUS,
        )
        sidebar.pack(side="left", fill="y", padx=(0, 8))
        sidebar.pack_propagate(False)

        shortcuts = [
            (t("shortcut_home"), os.path.expanduser("~")),
            (t("shortcut_downloads"), self._find_user_dir("Downloads", "下載")),
            (t("shortcut_documents"), self._find_user_dir("Documents", "文件")),
            (t("shortcut_desktop"), self._find_user_dir("Desktop", "桌面")),
        ]

        ctk.CTkLabel(
            sidebar, text=t("section_places"), font=Theme.font(11, bold=True),
            text_color="#71767B", anchor="w",
        ).pack(fill="x", padx=10, pady=(12, 6))

        for label, path in shortcuts:
            if path and os.path.isdir(path):
                ctk.CTkButton(
                    sidebar, text=label, font=Theme.font(12),
                    anchor="w", height=30, fg_color="transparent",
                    text_color="#A0A5AB", hover_color="#1F2226",
                    corner_radius=Theme.CORNER_RADIUS,
                    command=lambda p=path: self._navigate_to(p),
                ).pack(fill="x", padx=6, pady=2)

        self._list_frame = ctk.CTkScrollableFrame(
            main_frame, fg_color=Theme.BOX_BG,
            border_color=Theme.BORDER, border_width=1,
            corner_radius=Theme.CORNER_RADIUS,
        )
        self._list_frame.pack(side="right", fill="both", expand=True)

        # 3. Bottom action bar: selected path status + Cancel & Select buttons
        bottom_bar = ctk.CTkFrame(self, fg_color="transparent")
        bottom_bar.pack(fill="x", padx=16, pady=(8, 16))

        self._status_lbl = ctk.CTkLabel(
            bottom_bar, text="", font=Theme.font(11),
            text_color="#71767B", anchor="w",
        )
        self._status_lbl.pack(side="left", fill="x", expand=True, padx=(0, 10))

        outline_button(bottom_bar, t("btn_cancel"), self._on_cancel, width=80, height=32).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            bottom_bar, text=t("btn_select"), font=Theme.font(13),
            fg_color=Theme.TEXT, text_color=Theme.BG, hover_color=Theme.ACCENT_HOVER,
            corner_radius=Theme.CORNER_RADIUS, width=100, height=32,
            command=self._on_select,
        ).pack(side="right")

    @staticmethod
    def _find_user_dir(*names: str) -> str:
        home = os.path.expanduser("~")
        for n in names:
            p = os.path.join(home, n)
            if os.path.isdir(p):
                return p
        return ""

    def _on_up(self):
        parent = os.path.dirname(self._current_path)
        if parent and parent != self._current_path and os.path.isdir(parent):
            self._navigate_to(parent)

    def _on_path_entered(self):
        raw = self._path_entry.get().replace("\x00", "").strip()
        target = Config.resolve_path(raw) if raw else Config.BASE_PATH
        if os.path.isdir(target):
            self._navigate_to(target)
        else:
            self._path_entry.delete(0, "end")
            self._path_entry.insert(0, self._current_path)

    def _navigate_to(self, target: str):
        if not self.winfo_exists() or not os.path.isdir(target):
            return
        self._current_path = os.path.abspath(target)
        self._selected_path = self._current_path
        self._path_entry.delete(0, "end")
        self._path_entry.insert(0, self._current_path)
        self._status_lbl.configure(text=self._current_path)
        self._folder_buttons.clear()

        for w in self._list_frame.winfo_children():
            w.destroy()

        try:
            with os.scandir(self._current_path) as it:
                entries = sorted(
                    [e.name for e in it if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")],
                    key=lambda s: s.lower(),
                )
        except OSError:
            ctk.CTkLabel(
                self._list_frame, text=t("msg_permission_denied"),
                font=Theme.font(12), text_color="#FFADAD", anchor="w",
            ).pack(fill="x", padx=12, pady=12)
            return

        if not entries:
            ctk.CTkLabel(
                self._list_frame, text=t("msg_empty_directory"),
                font=Theme.font(12), text_color="#71767B", anchor="w",
            ).pack(fill="x", padx=12, pady=12)
            return

        for name in entries:
            full = os.path.join(self._current_path, name)
            btn = ctk.CTkButton(
                self._list_frame, text=f"📁  {name}",
                anchor="w", height=32, font=Theme.font(12),
                fg_color="transparent", hover_color=Theme.BTN_HOVER,
                text_color=Theme.TEXT, corner_radius=Theme.CORNER_RADIUS,
            )
            btn.configure(command=lambda p=full, b=btn: self._on_folder_click(p, b))
            btn.bind("<Double-Button-1>", lambda e, p=full: self.after_idle(self._navigate_to, p))
            btn.pack(fill="x", padx=4, pady=1)
            self._folder_buttons.append(btn)

    def _on_folder_click(self, path: str, clicked_btn: ctk.CTkButton | None = None):
        self._selected_path = path
        self._status_lbl.configure(text=path)
        for b in self._folder_buttons:
            b.configure(fg_color="#1F2226" if b is clicked_btn else "transparent")

    def _on_select(self):
        self.result = self._selected_path or self._current_path
        self.destroy()

    def _on_cancel(self):
        self.result = ""
        self.destroy()

    def show(self) -> str:
        self.wait_window()
        return self.result


def _run_without_freezing(parent, cmd: list[str]) -> subprocess.CompletedProcess | None:
    """subprocess.run(cmd) on a worker thread while the Tk event loop keeps running.

    Run directly, the GUI froze for as long as the external dialog stayed open (no repaints, no log
    updates, and the window manager may offer to kill the "not responding" app). The wait is on a hidden
    proxy window: wait_window() also returns if the app is closed meanwhile (wait_variable() would hang
    forever); the result is then None.
    """
    box = {}

    def work():
        try:
            # surrogateescape: a folder name that is not valid UTF-8 (Windows archives) must reach
            # _apply_dir, which explains it; a strict decode failed here and the dialog silently did nothing.
            box["res"] = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, errors="surrogateescape", check=False, env=host_env())
        except OSError as e:
            box["err"] = e

    worker = threading.Thread(target=work, daemon=True)
    proxy = tk.Toplevel(parent)
    proxy.withdraw()
    pending = None

    def poll():
        nonlocal pending
        if worker.is_alive():
            pending = proxy.after(50, poll)
        else:
            pending = None
            proxy.destroy()

    def on_destroy(_event):
        # Destroyed early (a window was closed meanwhile): a timer left behind would call a deleted command.
        if pending is not None:
            proxy.tk.call("after", "cancel", pending)

    proxy.bind("<Destroy>", on_destroy)
    worker.start()
    pending = proxy.after(50, poll)
    parent.wait_window(proxy)
    if "err" in box:
        raise box["err"]
    return box.get("res")


def _ask_directory(parent, initialdir: str, title: str) -> str:
    """Open the system native directory chooser (zenity/kdialog on Linux), with dark Tk fallback."""
    choosers = (    # tried in order; to support another tool, add its command line here
        ["zenity", "--file-selection", "--directory", f"--title={title}",
         f"--filename={os.path.join(initialdir, '')}"],
        ["kdialog", "--getexistingdirectory", initialdir, "--title", title],
    )
    for cmd in choosers:
        if not shutil.which(cmd[0]):
            continue
        try:
            res = _run_without_freezing(parent, cmd)
        except OSError:
            continue
        if res is None:              # the application was closed while the dialog was open
            return ""
        if res.returncode == 0:
            return res.stdout.rstrip("\r\n")
        if res.returncode == 1:      # cancelled
            return ""
    return DirectoryChooserDialog(parent, initialdir=initialdir, title=title).show()


# --- Settings Dialog ---
class SettingsDialog(_Dialog):
    _W, _H = 820, 520
    _MIN_SIZE = (720, 440)
    _MODAL = False

    def __init__(self, parent, logger: logging.Logger | None = None):
        super().__init__(parent, t("settings_title"))
        self._logger = logger
        self._active_tab = "General"
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._applying_dir = False
        self._browsing = False

        self.grid_columnconfigure(0, weight=0, minsize=140)
        self.grid_columnconfigure(1, weight=0, minsize=1)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_content_area()
        self._show_tab("General")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.focus()

    def _on_close(self):
        self._commit_pending_dir()
        self.destroy()

    def _downloading(self) -> bool:
        return getattr(self.master, "_is_downloading", False)

    def _set_dir_entry(self, text: str):
        """Shows *text* in the folder entry (only exists while the General tab is displayed)."""
        if hasattr(self, "_dir_entry") and self._dir_entry.winfo_exists():
            self._dir_entry.delete(0, "end")
            self._dir_entry.insert(0, text)

    def _restore_dir_entry(self):
        self._set_dir_entry(Config.DOWNLOAD_DIR)

    def _set_option(self, name: str, value: str):
        """Puts *value* back into the option menu stored as self.<name> (if it is displayed)."""
        menu = getattr(self, name, None)
        if menu is not None and menu.winfo_exists():
            menu.set(value)

    def _save_settings(self) -> bool:
        """Config.save_settings(), reporting a failure (the change then lasts only until the app closes)."""
        if Config.save_settings():
            return True
        if self._logger:
            self._logger.error("Cannot save %s: the change lasts only until the app is closed.", Config.SETTINGS_FILE)
        return False

    def _refuse_while_downloading(self, message_key: str, undo) -> bool:
        """Settings must not change during a download: undo the edit, tell the user, return True."""
        if not self._downloading():
            return False
        undo()
        CustomMessageBox(self, t("dlg_notice"), t(message_key)).show()
        return True

    def _commit_pending_dir(self):
        if hasattr(self, "_dir_entry") and self._dir_entry.winfo_exists():
            if self._downloading():
                self._restore_dir_entry()
                return
            raw = self._dir_entry.get()
            target = Config.resolve_path(raw)
            if target != Config.DOWNLOAD_DIR:
                self._apply_dir(raw)

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=140, corner_radius=0, fg_color="#0D0E10", border_width=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.pack_propagate(False)
        sidebar.grid_propagate(False)

        # 1px vertical divider separating sidebar and content area
        ctk.CTkFrame(self, width=1, corner_radius=0, fg_color=Theme.BORDER).grid(row=0, column=1, sticky="ns")

        self._sidebar_header_lbl = ctk.CTkLabel(
            sidebar, text=t("settings_sidebar_header"), font=Theme.font(22, bold=True),
            text_color=Theme.TEXT, anchor="w"
        )
        self._sidebar_header_lbl.pack(fill="x", padx=14, pady=(24, 18))

        for key in ("General", "Advanced", "About"):
            btn = ctk.CTkButton(
                sidebar, text=t(f"tab_{key.lower()}"), font=Theme.font(13),
                anchor="w", width=60, height=36, corner_radius=Theme.CORNER_RADIUS,
                fg_color="transparent", text_color="#A0A5AB",
                hover_color="#1F2226",
                command=lambda k=key: self._show_tab(k)
            )
            btn.pack(fill="x", padx=10, pady=2)
            self._nav_buttons[key] = btn

    def _build_content_area(self):
        self._content_container = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self._content_container.grid(row=0, column=2, sticky="nsew", padx=28, pady=24)
        self._content_container.grid_columnconfigure(0, weight=1)

    def _add_page_header(self, title_key: str):
        ctk.CTkLabel(
            self._content_container, text=t(title_key),
            font=Theme.font(22, bold=True), text_color=Theme.TEXT, anchor="w"
        ).pack(fill="x", pady=(0, 20))

    def _add_section_card(self, section_key: str, bottom_pad: int = 16) -> ctk.CTkFrame:
        ctk.CTkLabel(
            self._content_container, text=t(section_key),
            font=Theme.font(13, bold=True), text_color=Theme.TEXT, anchor="w"
        ).pack(fill="x", pady=(0, 8))
        card = ctk.CTkFrame(
            self._content_container, fg_color=Theme.BOX_BG,
            border_width=1, border_color=Theme.BORDER, corner_radius=Theme.CORNER_RADIUS
        )
        card.pack(fill="x", pady=(0, bottom_pad))
        return card

    @staticmethod
    def _add_card_row(card: ctk.CTkFrame, title: str, subtitle: str = "", pady: int = 14) -> ctk.CTkFrame:
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=pady)
        row.grid_columnconfigure(0, weight=1)
        row.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(left, text=title, font=Theme.font(13, bold=True), text_color=Theme.TEXT, anchor="w").pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(left, text=subtitle, font=Theme.font(11), text_color="#888888", anchor="w").pack(anchor="w", pady=(2, 0))
        return row

    def _show_tab(self, tab_key: str):
        self._commit_pending_dir()
        self._active_tab = tab_key
        for k, btn in self._nav_buttons.items():
            active = (k == tab_key)
            btn.configure(
                fg_color=Theme.BOX_BG if active else "transparent",
                text_color=Theme.TEXT if active else "#A0A5AB",
                font=Theme.font(13, bold=active),
            )

        for child in self._content_container.winfo_children():
            child.destroy()

        renderers = {
            "General": self._render_general,
            "Advanced": self._render_advanced,
            "About": self._render_about,
        }
        if tab_key in renderers:
            renderers[tab_key]()

    def _render_general(self):
        self._add_page_header("tab_general")

        # Section 1: Storage
        storage_card = self._add_section_card("section_storage", bottom_pad=16)
        item_row = self._add_card_row(storage_card, t("label_download_dir"), t("desc_download_dir"))

        btn_row = ctk.CTkFrame(item_row, fg_color="transparent")
        btn_row.grid(row=0, column=1, sticky="e", padx=(10, 0))
        outline_button(btn_row, t("btn_browse"), self._on_browse, 72, 30, 11).pack(side="left", padx=(0, 6))
        outline_button(btn_row, t("btn_open"), self._on_open_folder, 64, 30, 11).pack(side="left", padx=(0, 6))
        outline_button(btn_row, t("btn_default"), self._on_reset, 70, 30, 11).pack(side="left")

        ctk.CTkFrame(storage_card, fg_color=Theme.BORDER, height=1).pack(fill="x", padx=16, pady=(0, 12))

        entry_frame = ctk.CTkFrame(storage_card, fg_color="transparent")
        entry_frame.pack(fill="x", padx=16, pady=(0, 14))
        entry_frame.grid_columnconfigure(0, weight=1)

        self._dir_entry = ctk.CTkEntry(
            entry_frame, font=Theme.font(12),
            fg_color="#0D0E10", text_color=Theme.TEXT,
            border_color=Theme.BORDER, border_width=1, height=34,
            corner_radius=Theme.CORNER_RADIUS
        )
        self._dir_entry.grid(row=0, column=0, sticky="ew")
        self._dir_entry.insert(0, Config.DOWNLOAD_DIR)
        self._dir_entry.bind("<FocusOut>", lambda e: self._on_focus_out())
        self._dir_entry.bind("<Return>", lambda e: self._on_save())

        self._status_lbl = ctk.CTkLabel(
            storage_card, text=t("status_autosaved_default"),
            font=Theme.font(10), text_color="#555555", anchor="w"
        )
        self._status_lbl.pack(fill="x", padx=16, pady=(0, 12))

        # Section 2: Interface
        lang_card = self._add_section_card("section_interface", bottom_pad=12)
        lang_row = self._add_card_row(lang_card, t("label_language"), t("desc_language"))
        self._lang_opt = option_menu(
            lang_row, list(LANGUAGE_OPTIONS.keys()),
            LANGUAGE_LABELS.get(Config.LANGUAGE, "English"),
            self._on_language_change, width=120
        )
        self._lang_opt.grid(row=0, column=1, sticky="e")

    def _render_advanced(self):
        self._add_page_header("tab_advanced")

        card1 = self._add_section_card("section_network")
        row1 = self._add_card_row(card1, t("label_max_retries"), t("desc_max_retries"))
        self._retry_opt = option_menu(
            row1, ["1", "2", "3", "5"], str(Config.MAX_RETRIES),
            self._on_retry_change, width=80
        )
        self._retry_opt.grid(row=0, column=1, sticky="e")

        card2 = self._add_section_card("section_media")
        row2 = self._add_card_row(card2, t("label_container_format"), t("desc_container_format"))
        self._fmt_opt = option_menu(
            row2, list(Config.SUPPORTED_FORMATS), Config.MERGE_FORMAT,
            self._on_format_change, width=90
        )
        self._fmt_opt.grid(row=0, column=1, sticky="e")

    def _render_about(self):
        self._add_page_header("tab_about")
        card = self._add_section_card("section_env")

        rows = [
            (t("about_version_title"), __version__),
            (t("about_ytdlp_title"),
             t("badge_ready") if os.path.exists(Config.YT_DLP_EXE) else t("badge_missing")),
            (t("about_ffmpeg_title"),
             t("badge_ready") if os.path.exists(Config.FFMPEG_EXE) else t("badge_missing")),
            (t("about_env_title"),
             platform.system()),
        ]

        for i, (title, badge) in enumerate(rows):
            r = self._add_card_row(card, title, pady=14)
            ctk.CTkLabel(r, text=badge, font=Theme.font(12), text_color=Theme.TEXT, anchor="e").grid(row=0, column=1, sticky="e")
            if i < len(rows) - 1:
                ctk.CTkFrame(card, fg_color=Theme.BORDER, height=1).pack(fill="x", padx=16)

    def _apply_dir(self, raw_path: str, exact: bool = False):
        """Makes *raw_path* the download folder. Typed text is expanded and trimmed like in a shell; a folder that was
        chosen (*exact*: Browse, Default) is a real path and is taken as it is, so a name that yt-dlp would change is
        refused below instead of being quietly replaced by its trimmed look-alike."""
        if getattr(self, "_applying_dir", False):
            return
        self._applying_dir = True
        try:
            if self._refuse_while_downloading("msg_dir_change_busy", self._restore_dir_entry):
                return
            path = os.path.abspath(raw_path) if exact else Config.resolve_path(raw_path)
            if not os.path.isdir(path):
                try:
                    os.makedirs(path, exist_ok=True)
                except (OSError, ValueError) as e:
                    self._restore_dir_entry()
                    CustomMessageBox(self, t("dlg_error"), t("msg_dir_create_failed", error=e), mode="error").show()
                    return
            problem = folder_problem(path)
            if problem:
                # Anything accepted here is shown as "✓ Auto-saved" and would only fail later, URL by URL.
                self._restore_dir_entry()
                if problem[0] == "name":
                    shown = path.encode("utf-8", "replace").decode()      # a bad encoding cannot be displayed
                    message = t("msg_dir_bad_name", path=shown)
                else:
                    message = t("msg_dir_not_writable", error=problem[1])
                CustomMessageBox(self, t("dlg_error"), message, mode="error").show()
                return
            Config.DOWNLOAD_DIR = path
            Config.MISSING_DOWNLOAD_DIR = None        # an explicit choice replaces a folder that was missing
            saved = self._save_settings()
            self._set_dir_entry(path)
            if hasattr(self, "_status_lbl") and self._status_lbl.winfo_exists():
                self._status_lbl.configure(text=t("status_autosaved_path", path=Config.DOWNLOAD_DIR) if saved
                                           else t("status_save_failed"))
            if self._logger:
                self._logger.info("Download folder set to: %s", Config.DOWNLOAD_DIR)
        finally:
            self._applying_dir = False

    def _on_browse(self):
        if getattr(self, "_browsing", False):
            return
        if self._refuse_while_downloading("msg_dir_change_busy", self._restore_dir_entry):
            return
        curr = (self._dir_entry.get().strip() if hasattr(self, "_dir_entry") and self._dir_entry.winfo_exists() else "") or Config.DOWNLOAD_DIR
        initial = Config.resolve_path(curr)
        if not os.path.isdir(initial):
            initial = Config.BASE_PATH
        self._browsing = True
        try:
            chosen = _ask_directory(self, initial, t("dlg_select_dir"))
        finally:
            self._browsing = False
        if chosen and self.winfo_exists():
            self._apply_dir(chosen, exact=True)
            self.lift()
            self.focus()

    def _on_reset(self):
        self._apply_dir(Config.BASE_PATH, exact=True)

    def _on_open_folder(self):
        self._commit_pending_dir()
        folder = Config.DOWNLOAD_DIR
        if not os.path.exists(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                pass
        try:
            proc = subprocess.Popen(
                ["xdg-open", folder],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=host_env(),
            )
            threading.Thread(target=proc.wait, daemon=True).start()
        except OSError:
            pass

    def _on_focus_out(self):
        if getattr(self, "_browsing", False) or getattr(self, "_applying_dir", False):
            return
        if hasattr(self, "_dir_entry") and self._dir_entry.winfo_exists():
            raw = self._dir_entry.get()
            target = Config.resolve_path(raw)
            if target != Config.DOWNLOAD_DIR and not os.path.isdir(target):
                # Moving the focus (half-typed path, Alt+Tab) must not create folders: that left empty
                # folders behind and saved them as the download folder. A new folder is created by an
                # explicit action: Enter, Browse, closing the page, switching tabs or starting a download.
                if hasattr(self, "_status_lbl") and self._status_lbl.winfo_exists():
                    self._status_lbl.configure(text=t("status_folder_pending"))
                return
            if target != Config.DOWNLOAD_DIR or not os.path.isdir(Config.DOWNLOAD_DIR):
                self._apply_dir(raw)
            elif raw != Config.DOWNLOAD_DIR:
                self._restore_dir_entry()

    def _on_save(self):
        if hasattr(self, "_dir_entry") and self._dir_entry.winfo_exists():
            self._apply_dir(self._dir_entry.get())

    def _on_retry_change(self, value: str):
        if self._refuse_while_downloading(
                "msg_settings_busy", lambda: self._set_option("_retry_opt", str(Config.MAX_RETRIES))):
            return
        try:
            val = int(value)
            if 1 <= val <= 10:
                Config.MAX_RETRIES = val
                self._save_settings()
                if self._logger:
                    self._logger.info("Max attempts set to: %d", Config.MAX_RETRIES)
        except ValueError:
            pass

    def _on_format_change(self, value: str):
        if self._refuse_while_downloading(
                "msg_settings_busy", lambda: self._set_option("_fmt_opt", Config.MERGE_FORMAT)):
            return
        if value in Config.SUPPORTED_FORMATS:
            Config.MERGE_FORMAT = value
            self._save_settings()
            if self._logger:
                self._logger.info("Merge format set to: %s", Config.MERGE_FORMAT)

    def _refresh_language(self):
        self.title(t("settings_title"))
        self._sidebar_header_lbl.configure(text=t("settings_sidebar_header"))
        for key, btn in self._nav_buttons.items():
            btn.configure(text=t(f"tab_{key.lower()}"))
        self._show_tab(self._active_tab)

    def _on_language_change(self, value: str):
        new_lang = LANGUAGE_OPTIONS.get(value, value if value in Config.SUPPORTED_LANGUAGES else None)
        if not new_lang or new_lang == Config.LANGUAGE:
            return
        self._commit_pending_dir()
        Config.LANGUAGE = new_lang
        self._save_settings()
        if self._logger:
            self._logger.info("Language set to: %s", LANGUAGE_LABELS.get(Config.LANGUAGE, Config.LANGUAGE))
        self._refresh_language()
        if hasattr(self.master, "refresh_language"):
            self.master.refresh_language()


# --- App ---
class App(ctk.CTk):
    _WIDTH = 880
    _HEIGHT = 700
    _POLL_MS = 100

    def __init__(self):
        Config.load_settings()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.configure(fg_color=Theme.BG)

        Theme.resolve_fonts()

        self.title("X-Downloader")
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
        self.minsize(680, 580)
        self._is_downloading = False
        self._updating = False
        self._closing = False
        self._confirming = False   # True when the exit-confirmation dialog is open.
        self._poll_id = None
        self._settings_window = None     # child windows / popup menu while open
        self._log_window = None
        self._custom_menu = None
        # Callables queued by worker threads, executed in the Tk main loop.
        self._ui_calls: queue.SimpleQueue = queue.SimpleQueue()

        self._build_layout()
        try:
            self._init_logger()
        except OSError as e:        # the log file cannot be opened: e.g. left by a run with sudo, like the lock file
            CustomMessageBox(self, t("dlg_startup_failed"), t("msg_log_failed", error=e), mode="error").show()
            self._shutdown_ui()
            sys.exit(1)
        self._poll_ui()

        self._lock = LockManager(self._logger)
        try:
            locked, message = self._lock.acquire(), t("msg_already_running")
        except OSError as e:
            locked, message = False, t("msg_lock_failed", error=e)
        if not locked:
            CustomMessageBox(self, t("dlg_startup_failed"), message, mode="error").show()
            self._shutdown_ui()
            sys.exit(1)

        # We hold the lock, so no other instance runs: a yt-dlp still alive is an orphan of one that was killed.
        orphans = ProcessManager.kill_orphans(Config.YT_DLP_EXE, Config.YT_DLP_EXE + BIN_SUFFIX)
        if orphans:
            self._logger.warning("Stopped %d leftover yt-dlp process group(s) from an earlier run.", orphans)

        # What a write killed before its rename left behind (Config.load_settings() does the same for settings.json).
        for path in (Config.LIST_FILE, Config.PROCESSING_FILE, Config.FAILED_FILE,
                     *(os.path.join(Config.BASE_PATH, site.cookies_name) for site in SITES)):
            remove_stale_temp_files(path)

        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._install_signal_handlers()
        self.bind_all("<Button-1>", self._check_menu_close, add="+")
        self._load_initial_list()

        # Disable start button during update
        self._updating = True
        self._start_btn.configure(state="disabled", text=t("btn_checking_updates"))
        threading.Thread(target=self._startup_update, daemon=True).start()

    # --- Termination signals ---
    def _install_signal_handlers(self):
        """kill / pkill / system shutdown (SIGTERM) and Ctrl+C (SIGINT) used to end the process at once,
        while yt-dlp (in its own session) kept downloading as an orphan. Close like the window's [X] instead.

        Only signals still at their default action are taken over: nohup / '&' may have made them ignored.
        """
        self._prev_handlers = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            if signal.getsignal(sig) in (signal.SIG_DFL, signal.default_int_handler):
                self._prev_handlers[sig] = signal.signal(sig, self._on_signal)

    def _restore_signal_handlers(self):
        for sig, prev in getattr(self, "_prev_handlers", {}).items():
            if signal.getsignal(sig) == self._on_signal:
                signal.signal(sig, prev)
        self._prev_handlers = {}

    def _on_signal(self, signum, frame):
        # Runs in the main thread between bytecodes (Tk returns to Python at least every _POLL_MS);
        # the actual shutdown is deferred to the event loop instead of interrupting whatever was running.
        try:
            self.after(0, self._close_on_signal, signum)
        except tk.TclError:
            pass   # already being destroyed

    def _close_on_signal(self, signum):
        self._logger.warning("Received signal %d, closing.", signum)
        self._on_closing(confirm=False)

    # --- Thread -> UI bridge ---
    def _call_in_ui(self, fn, *args):
        """Thread-safe: schedules fn(*args) to run in the Tk main loop."""
        self._ui_calls.put((fn, args))

    def _poll_ui(self):
        while True:
            try:
                fn, args = self._ui_calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                self._logger.exception("UI callback failed")
        if hasattr(self, "_gui_handler"):
            self._gui_handler.drain()
        if not self._closing:
            try:
                self._poll_id = self.after(self._POLL_MS, self._poll_ui)
            except tk.TclError:
                pass

    def _shutdown_ui(self):
        self._closing = True
        self._restore_signal_handlers()
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        if hasattr(self, "_progress"):
            try:
                self._progress.stop()
            except Exception:
                pass
        if hasattr(self, "_gui_handler"):
            self._logger.removeHandler(self._gui_handler)
        try:
            # Cancel the Tcl timer only: Misc.after_cancel() also deletes the Tcl command that a
            # child widget still tracks in its _tclCommands, which makes that child's destroy()
            # raise "can't delete Tcl command" and leaves a half-destroyed, unclosable window.
            for after_id in self.tk.call("after", "info"):
                self.tk.call("after", "cancel", after_id)
        except Exception:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _startup_update(self):
        try:
            EnvManager(self._logger).update_yt_dlp()
        except Exception:
            self._logger.warning("yt-dlp update check failed.", exc_info=True)
        finally:
            # Restore button after update (always, even if the update crashed)
            self._call_in_ui(self._on_update_done)

    def _on_update_done(self):
        self._updating = False
        if not self._is_downloading:
            self._start_btn.configure(state="normal", text=t("btn_download"))

    def _load_initial_list(self):
        urls = []
        try:
            queue_mgr = QueueManager(self._logger)
            unfinished = queue_mgr.load_unfinished()
            pending = read_lines(Config.LIST_FILE)
            urls = merge_unique(unfinished, pending)
            if unfinished:
                # Hand the crash-resume queue over to list.txt (the editable list). Otherwise
                # a stale .processing.list was reloaded on every launch, even after the user
                # cleared the list.
                atomic_write_lines(Config.LIST_FILE, urls)
                queue_mgr.clear()
                source = ".processing.list (crash-resume) + list.txt"
            else:
                source = "list.txt"
        except Exception as e:
            self._logger.error("Failed to load initial list: %s", e)
            return

        if urls:
            self._listbox.insert("end", "\n".join(urls) + "\n")
            self._refresh_count()
            self._logger.info("Loaded %d unfinished URL(s) from %s.", len(urls), source)

    def _save_list(self):
        try:
            atomic_write_lines(Config.LIST_FILE, self._get_valid_urls())
        except OSError as e:
            self._logger.error("Cannot save list.txt: %s", e)

    def _on_closing(self, confirm: bool = True):
        """confirm=False (closing on a signal): no dialogs, they would block the shutdown."""
        if self._closing or (confirm and self._confirming):
            return
        if confirm and self._is_downloading:
            self._confirming = True   # wait_window() keeps the event loop running
            try:
                answer = CustomMessageBox(
                    self, t("dlg_warning"),
                    t("msg_force_close"),
                    mode="askyesno"
                ).show()
            finally:
                self._confirming = False
            if not answer:
                return
        if confirm and _alive(self._settings_window):
            self._confirming = True
            try:
                self._settings_window._commit_pending_dir()
            finally:
                self._confirming = False
        if self._is_downloading:
            self._logger.warning("User forced termination. Cleaning up background processes...")
        else:
            # Only save UI list to list.txt if we are not actively downloading.
            # During download, QueueManager handles the queue state via .processing.list
            self._save_list()

        ProcessManager.kill_all()

        self._logger.info("Application closed.")
        if hasattr(self, "_lock"):
            self._lock.release()
        self._shutdown_ui()

    # --- Layout ---
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_list_frame()
        self._build_control_frame()
        self._build_log_frame()

        self._toggle_widgets = [self._clear_btn, self._settings_btn, self._listbox]

    def _section_header(self, parent, row: int) -> ctk.CTkFrame:
        """A transparent header row: label on the left, a button bar on the right."""
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=row, column=0, padx=0, pady=(12, 4), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        return header

    def _build_header(self):
        frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        frame.grid(row=0, column=0, padx=24, pady=(18, 6), sticky="ew")

        ctk.CTkLabel(
            frame, text="X-Downloader",
            font=Theme.font(20, bold=True), text_color=Theme.TEXT, anchor="w"
        ).pack(anchor="w")

    def _build_list_frame(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=1, column=0, padx=24, pady=(0, 6), sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        header = self._section_header(frame, 0)
        self._count_label = ctk.CTkLabel(
            header, text=t("queue_count", count=0),
            font=Theme.font(), text_color=Theme.TEXT, anchor="w"
        )
        self._count_label.grid(row=0, column=0, sticky="w")

        btn_bar = ctk.CTkFrame(header, fg_color="transparent")
        btn_bar.grid(row=0, column=1, sticky="e")

        self._clear_btn = outline_button(btn_bar, t("btn_clear_list"), self._on_clear_list, 92, 30, 12)
        self._clear_btn.pack(side="left", padx=(0, 8))

        self._settings_btn = outline_button(btn_bar, t("btn_settings"), self._on_settings, 80, 30, 12)
        self._settings_btn.pack(side="left")

        self._listbox = text_box(frame, wrap="none")
        self._listbox.grid(row=1, column=0, padx=0, pady=(0, 14), sticky="nsew")
        self._listbox.bind("<KeyRelease>", lambda e: self._refresh_count())
        self._listbox.bind("<<Paste>>", lambda e: self.after_idle(self._refresh_count))
        # Middle-click (X11 primary selection) paste does not fire <KeyRelease>.
        self._listbox.bind("<ButtonRelease-2>", lambda e: self.after_idle(self._refresh_count))
        self._listbox.bind("<ButtonRelease-3>", self._on_listbox_right_click)

    def _build_control_frame(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=2, column=0, padx=24, pady=(0, 6), sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

        self._progress = ctk.CTkProgressBar(frame, mode="indeterminate", height=6,
                                            fg_color=Theme.BORDER, progress_color=Theme.ACCENT,
                                            corner_radius=Theme.CORNER_RADIUS)
        self._progress.grid(row=0, column=0, padx=(0, 14), sticky="ew")
        self._progress.set(0)

        self._start_btn = ctk.CTkButton(
            frame, text=t("btn_download"), width=148, height=44,
            font=Theme.font(14, bold=True),
            fg_color=Theme.ACCENT, hover_color=Theme.ACCENT_HOVER, text_color="#000000",
            corner_radius=Theme.CORNER_RADIUS,
            command=self._on_start_download
        )
        self._start_btn.grid(row=0, column=1)

    def _build_log_frame(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=3, column=0, padx=24, pady=(0, 20), sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

        header = self._section_header(frame, 0)
        self._log_label = ctk.CTkLabel(
            header, text=t("label_exec_log"), font=Theme.font(), text_color=Theme.TEXT, anchor="w"
        )
        self._log_label.grid(row=0, column=0, sticky="w")

        btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        btn_frame.grid(row=0, column=1, sticky="e")
        self._expand_log_btn = outline_button(btn_frame, t("btn_expand"), self._on_expand_log, 70, 26, 11)
        self._expand_log_btn.pack(side="left", padx=(0, 8))
        self._clear_log_btn = outline_button(btn_frame, t("btn_clear"), self._on_clear_log, 66, 26, 11)
        self._clear_log_btn.pack(side="left")

        self._log_box = text_box(frame, height=150, state="disabled", wrap="word")
        self._log_box.grid(row=1, column=0, padx=0, pady=(0, 14), sticky="ew")

    def refresh_language(self):
        self._close_custom_menu()
        self._refresh_count()
        self._clear_btn.configure(text=t("btn_clear_list"))
        self._settings_btn.configure(text=t("btn_settings"))
        self._start_btn.configure(text=self._start_btn_text())
        self._log_label.configure(text=t("label_exec_log"))
        self._expand_log_btn.configure(text=t("btn_expand"))
        self._clear_log_btn.configure(text=t("btn_clear"))
        if _alive(self._log_window):
            self._log_window.title(t("log_expanded_title"))
        if hasattr(self, "_gui_handler"):
            self._gui_handler.refresh_language()

    # --- Logger ---
    def _init_logger(self):
        logger = setup_logger()
        # Drop handlers left over from a previous App instance in this process.
        for h in [h for h in logger.handlers if isinstance(h, GUILogHandler)]:
            logger.removeHandler(h)
        self._gui_handler = GUILogHandler(self._log_box)
        self._gui_handler.setLevel(logging.DEBUG)
        logger.addHandler(self._gui_handler)
        self._logger = logger
        self._logger.info("X-Downloader GUI started.")
        self._logger.info("Download folder: %s", Config.DOWNLOAD_DIR)
        if Config.MISSING_DOWNLOAD_DIR:
            self._logger.warning("Saved download folder not found (drive not connected?): %s",
                                 Config.MISSING_DOWNLOAD_DIR)

    # --- Event Handlers ---
    def _get_valid_urls(self):
        return extract_urls(self._listbox.get("1.0", "end-1c"))

    def _on_context_paste(self):
        self._close_custom_menu()
        if self._is_downloading or self._listbox.cget("state") == "disabled":
            return
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        if not text:
            return
        # Keep every pasted URL on its own line; previously consecutive pastes were glued
        # together ("...status/1https://x.com/...") into one invalid URL.
        before = self._listbox.get("insert linestart", "insert")
        if before.strip():
            text = "\n" + text
        if not text.endswith("\n"):
            text += "\n"
        self._listbox.insert("insert", text)
        self._listbox.see("insert")
        self._refresh_count()

    def _close_custom_menu(self):
        if _alive(self._custom_menu):
            self._custom_menu.destroy()

    def _check_menu_close(self, event):
        if _alive(self._custom_menu):
            x, y = event.x_root, event.y_root
            mx = self._custom_menu.winfo_rootx()
            my = self._custom_menu.winfo_rooty()
            mw = self._custom_menu.winfo_width()
            mh = self._custom_menu.winfo_height()
            if not (mx <= x <= mx + mw and my <= y <= my + mh):
                self._close_custom_menu()

    def _on_listbox_right_click(self, event):
        try:
            if self._is_downloading or self._listbox.cget("state") == "disabled":
                return
            self._listbox.focus_set()
            self._close_custom_menu()
        except tk.TclError:
            return

        self._custom_menu = ctk.CTkFrame(
            self, bg_color=Theme.BOX_BG, fg_color=Theme.BOX_BG, border_color=Theme.BORDER,
            border_width=1, corner_radius=Theme.CORNER_RADIUS
        )

        def menu_item(text, command, pady):
            ctk.CTkButton(
                self._custom_menu, text=text, fg_color="transparent",
                text_color=Theme.TEXT, hover_color=Theme.BTN_HOVER,
                corner_radius=Theme.CORNER_RADIUS, height=32, anchor="w", command=command
            ).pack(fill="x", padx=4, pady=pady)

        menu_item(t("menu_paste_url"), self._on_context_paste, (4, 2))
        ctk.CTkFrame(self._custom_menu, fg_color=Theme.BORDER, height=1).pack(fill="x", padx=4, pady=2)
        menu_item(t("btn_clear_list"), self._on_clear_list, (2, 4))

        self._custom_menu.update_idletasks()
        mw = self._custom_menu.winfo_reqwidth()
        mh = self._custom_menu.winfo_reqheight()
        x = max(0, min(event.x_root - self.winfo_rootx(), self.winfo_width() - mw - 8))
        y = max(0, min(event.y_root - self.winfo_rooty(), self.winfo_height() - mh - 8))
        self._custom_menu.place(x=x, y=y)
        self._custom_menu.lift()

    def _on_clear_list(self):
        self._close_custom_menu()
        if self._is_downloading:
            return
        self._listbox.delete("1.0", "end")
        self._refresh_count()

    def _on_settings(self):
        if _alive(self._settings_window):
            self._settings_window.lift()
            self._settings_window.focus()
            return
        self._settings_window = SettingsDialog(self, logger=self._logger)

    def _on_clear_log(self):
        if hasattr(self, "_gui_handler"):
            self._gui_handler.clear()
        else:
            self._log_box.configure(state="normal")
            self._log_box.delete("1.0", "end")
            self._log_box.configure(state="disabled")

    def _on_expand_log(self):
        if _alive(self._log_window):
            self._log_window.lift()
            self._log_window.focus()
            return

        self._log_window = ctk.CTkToplevel(self)
        self._log_window.title(t("log_expanded_title"))
        self._log_window.geometry("900x650")
        self._log_window.minsize(600, 400)
        self._log_window.configure(fg_color=Theme.BG)
        self._log_window.focus()

        textbox = text_box(self._log_window, state="disabled", wrap="word")
        textbox.pack(fill="both", expand=True, padx=14, pady=14)

        # Flush queued records first so the copy and the live feed do not overlap/miss lines.
        self._gui_handler.drain()
        gui_handler = self._gui_handler
        gui_handler.add_textbox(textbox)
        log_window = self._log_window

        def on_close():
            gui_handler.remove_textbox(textbox)
            log_window.destroy()
            self._log_window = None

        self._log_window.protocol("WM_DELETE_WINDOW", on_close)

    def _refresh_count(self):
        valid_urls = self._get_valid_urls()
        self._count_label.configure(
            text=t("queue_count", count=len(valid_urls))
        )

    def _start_btn_text(self) -> str:
        if self._is_downloading:
            return t("btn_stop")
        return t("btn_checking_updates") if self._updating else t("btn_download")

    def _set_ui_state(self, downloading: bool):
        state = "disabled" if downloading else "normal"
        for w in self._toggle_widgets:
            w.configure(state=state)
        # While downloading the same button stops the download instead of being disabled.
        self._start_btn.configure(state="normal", text=t("btn_stop") if downloading else t("btn_download"))

    def _on_stop_download(self):
        self._logger.info("Download stopped by the user.")
        self._start_btn.configure(state="disabled")     # until the worker thread has wound down
        ProcessManager.cancel()

    def _on_start_download(self):
        if self._is_downloading:
            self._on_stop_download()
            return
        if self._updating:
            return
        if _alive(self._settings_window):
            self._settings_window._commit_pending_dir()
        urls = self._get_valid_urls()
        if not urls:
            sites = ", ".join(s.name for s in SITES)
            CustomMessageBox(self, t("dlg_notice"), t("msg_no_valid_url", sites=sites)).show()
            return

        self._close_custom_menu()
        self._is_downloading = True
        self._set_ui_state(downloading=True)
        self._progress.start()

        self._logger.info("=" * 44)
        self._logger.info("Starting download task for %d URL(s).", len(urls))

        ProcessManager.clear_cancel()
        DownloadThread(urls=urls, on_finish=self._on_download_done).start()

    def _on_download_done(self, pending: list[str]):
        # Called from the worker thread.
        self._call_in_ui(self._reset_controls, pending)

    def _reset_controls(self, pending: list[str] | None = None):
        self._is_downloading = False
        self._progress.stop()
        self._progress.set(0)
        # Re-enable first: a disabled CTkTextbox silently ignores insert/delete.
        self._set_ui_state(downloading=False)
        self._listbox.delete("1.0", "end")
        # Unfinished URLs (e.g. dependency download failed) are restored instead of lost.
        if pending:
            self._listbox.insert("end", "\n".join(pending) + "\n")
        self._refresh_count()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
