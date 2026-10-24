"""111477 Downloader – browse the index and send downloads to IDM."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import unquote

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import (  # noqa: E402
    BASE_URL, BROWSER_DIR, CACHE_DB, DATA_DIR, DEFAULT_SECTIONS, LOG_PATH,
    PROXY_URL, Settings,
)
from core.idm import IDM, find_idm  # noqa: E402
from core.index_client import (  # noqa: E402
    Entry, IndexClient, IndexError_, natural_key, normalize_path, parent_path,
    path_segments,
)
from core.resolver import Resolver  # noqa: E402
from core.store import Store  # noqa: E402

APP_TITLE = "111477 Downloader"
VERSION = "1.0"

# --------------------------------------------------------------------- palette
BG = "#0e1117"
PANEL = "#151a23"
CARD = "#1b212c"
CARD_HI = "#232b39"
BORDER = "#252d3a"
TEXT = "#e8edf6"
MUTED = "#8c98ad"
DIM = "#66718a"
ACCENT = "#4da6ff"
ACCENT_DARK = "#2b6fb3"
ON_ACCENT = "#06121f"
GREEN = "#5fd08a"
AMBER = "#f0b45a"
RED = "#f27d7d"
CHECKED_BG = "#182636"

UI_FONT = ("Segoe UI", 10)
UI_BOLD = ("Segoe UI Semibold", 10)
SMALL_FONT = ("Segoe UI", 9)
TITLE_FONT = ("Segoe UI Semibold", 17)
MONO_FONT = ("Consolas", 9)


def human_size(value: int | float | None) -> str:
    if value is None or value < 0:
        return "-"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} B"
    if size >= 100:
        return f"{size:.0f} {units[index]}"
    return f"{size:.1f} {units[index]}"


_BAD_CHARS = '<>:"/\\|?*'


def safe_name(name: str) -> str:
    cleaned = "".join("_" if ch in _BAD_CHARS or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.rstrip(" .")
    return cleaned[:180] or "unnamed"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def setup_logging() -> None:
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        encoding="utf-8",
    )


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings.load()
        self.store = Store(CACHE_DB)
        self.index = IndexClient(
            self.store,
            ttl_seconds=self.settings.index_ttl_minutes * 60,
            log=self.log,
        )
        self.idm = IDM(self.settings.idm_path)
        self.resolver = Resolver(self.store, self.settings, log=self.log, status=self.set_status)

        self.entries: list[Entry] = []
        self.selection: set[str] = set()
        self.sizes: dict[str, int] = {}
        self.current_path = "/"
        self.history: list[str] = []
        self.hist_pos = -1
        self.sort_col = "name"
        self.sort_desc = False
        self._load_token: object | None = None
        self._initialising = True
        self._filter_job: str | None = None
        self._render_gen = 0
        self._render_rows: list[Entry] = []
        self._render_pos = 0
        self._render_after: str | None = None
        self._busy = False
        self._buttons: list[ttk.Button] = []

        self.queue: queue.Queue = queue.Queue()
        self._setup_window()
        self._setup_style()
        self._build()
        self.after(70, self._drain)
        self.after(120, self._initial_load)
        self.after(80, self._apply_dark_titlebar)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._check_idm()

    # ------------------------------------------------------------------ window
    def _setup_window(self) -> None:
        self.title(APP_TITLE)
        self.geometry("1260x820")
        self.minsize(1000, 640)
        self.configure(bg=BG)
        try:
            icon = resource_path("assets/icon.png")
            if icon.exists():
                self._icon_image = tk.PhotoImage(file=str(icon))
                self.iconphoto(True, self._icon_image)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass


    def _apply_dark_titlebar(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0:
                    break
        except Exception:  # noqa: BLE001 - purely cosmetic
            pass

    def _setup_style(self) -> None:
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=UI_FONT,
                        bordercolor=BORDER, darkcolor=BG, lightcolor=BG,
                        troughcolor=BG, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Small.TLabel", background=BG, foreground=MUTED, font=SMALL_FONT)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=TITLE_FONT)
        style.configure("TButton", background=CARD, foreground=TEXT, bordercolor=BORDER,
                        focusthickness=0, relief="flat", padding=(11, 6), font=SMALL_FONT)
        style.map("TButton",
                  background=[("pressed", "#2a3444"), ("active", CARD_HI), ("disabled", "#171c25")],
                  foreground=[("disabled", DIM)])
        style.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT,
                        font=("Segoe UI Semibold", 9), padding=(14, 6), bordercolor=ACCENT)
        style.map("Accent.TButton",
                  background=[("pressed", "#3c8cd6"), ("active", "#66b6ff"), ("disabled", "#22303f")],
                  foreground=[("disabled", "#6b7a8f")])
        style.configure("Ghost.TButton", background=BG, foreground=MUTED, padding=(7, 4))
        style.map("Ghost.TButton", background=[("active", CARD)], foreground=[("active", TEXT)])
        style.configure("TEntry", fieldbackground=CARD, foreground=TEXT, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=5)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                        arrowcolor=MUTED, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                        padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", CARD)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", CARD)],
                  selectforeground=[("readonly", TEXT)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=28, borderwidth=0, relief="flat", font=("Segoe UI", 9))
        style.map("Treeview",
                  background=[("selected", "#22344c")],
                  foreground=[("selected", TEXT)])
        style.configure("Treeview.Heading", background=CARD, foreground=MUTED,
                        relief="flat", borderwidth=0, font=("Segoe UI Semibold", 9),
                        padding=(8, 7))
        style.map("Treeview.Heading", background=[("active", CARD_HI)], foreground=[("active", TEXT)])
        style.configure("Vertical.TScrollbar", background=CARD, troughcolor=PANEL,
                        bordercolor=PANEL, arrowcolor=MUTED, width=11,
                        lightcolor=CARD, darkcolor=CARD, gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", CARD_HI), ("pressed", CARD_HI)])
        style.configure("Horizontal.TScrollbar", background=CARD, troughcolor=PANEL,
                        bordercolor=PANEL, arrowcolor=MUTED, gripcount=0,
                        lightcolor=CARD, darkcolor=CARD)
        style.configure("TProgressbar", background=ACCENT, troughcolor=CARD,
                        bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)

    # -------------------------------------------------------------------- build
    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        self._build_header()
        self._build_banner()
        self._build_nav()
        self._build_breadcrumb()
        self._build_toolbar()
        self._build_tree()
        self._build_actions()
        self._build_status()
        self._build_log()

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(20, 14, 20, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(2, weight=1)

        mark = tk.Canvas(header, width=38, height=38, bg=BG, highlightthickness=0)
        mark.create_rectangle(2, 2, 36, 36, outline=ACCENT, width=1)
        mark.create_text(19, 20, text="11", fill=ACCENT, font=("Segoe UI Semibold", 11))
        mark.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))

        ttk.Label(header, text="111477 Downloader", style="Title.TLabel").grid(
            row=0, column=1, sticky="sw")
        ttk.Label(header, text="Browse the index · mint links · hand off to IDM",
                  style="Small.TLabel").grid(row=1, column=1, sticky="nw")

        right = ttk.Frame(header)
        right.grid(row=0, column=3, rowspan=2, sticky="e")

        self.idm_badge = tk.Label(right, text="IDM", bg=CARD, fg=MUTED, font=SMALL_FONT,
                                  padx=10, pady=4)
        self.idm_badge.pack(side="left", padx=(0, 8))

        ttk.Button(right, text="Help", style="Ghost.TButton",
                   command=self._show_help).pack(side="left")
        ttk.Button(right, text="Settings", command=self._open_settings).pack(side="left", padx=(6, 0))

    def _build_banner(self) -> None:
        self.banner = tk.Frame(self, bg="#2a2113", highlightthickness=1,
                               highlightbackground="#4a3a1a")
        self.banner.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 0))
        self.banner.grid_remove()
        self.banner_label = tk.Label(
            self.banner, text="", bg="#2a2113", fg="#f3c97a", font=SMALL_FONT,
            justify="left", anchor="w", padx=12, pady=7,
        )
        self.banner_label.pack(side="left", fill="x", expand=True)
        self.banner_actions = tk.Frame(self.banner, bg="#2a2113")
        self.banner_actions.pack(side="right", padx=(0, 8))
        self.banner.bind("<Configure>", self._on_banner_resize)

    def _on_banner_resize(self, event) -> None:
        self.banner_label.configure(wraplength=max(event.width - 380, 420))

    def _build_nav(self) -> None:
        nav = ttk.Frame(self, padding=(20, 10, 20, 0))
        nav.grid(row=2, column=0, sticky="ew")
        nav.grid_columnconfigure(5, weight=1)

        self.section_var = tk.StringVar()
        self.section_box = ttk.Combobox(
            nav, textvariable=self.section_var,
            values=["Root", *DEFAULT_SECTIONS],
            state="readonly", width=14,
        )
        self.section_box.set("Root")
        self.section_box.grid(row=0, column=0, padx=(0, 10))
        self.section_box.bind("<<ComboboxSelected>>", self._on_section)

        self.back_btn = ttk.Button(nav, text="‹", width=3, command=self._go_back)
        self.back_btn.grid(row=0, column=1)
        self.fwd_btn = ttk.Button(nav, text="›", width=3, command=self._go_forward)
        self.fwd_btn.grid(row=0, column=2, padx=(2, 2))
        ttk.Button(nav, text="↑", width=3, command=self._go_up).grid(row=0, column=3)
        ttk.Button(nav, text="↻", width=3, command=self._refresh).grid(row=0, column=4, padx=(2, 10))

        self.path_var = tk.StringVar(value="/")
        path_entry = tk.Entry(
            nav, textvariable=self.path_var, bg=CARD, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, font=("Consolas", 9),
        )
        path_entry.grid(row=0, column=5, sticky="ew", ipady=6)
        path_entry.bind("<Return>", lambda _e: self._go_typed())

        ttk.Button(nav, text="Go", command=self._go_typed).grid(row=0, column=6, padx=(8, 0))

    def _build_breadcrumb(self) -> None:
        self.crumb = tk.Frame(self, bg=BG)
        self.crumb.grid(row=3, column=0, sticky="ew", padx=20, pady=(6, 0))

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(20, 10, 20, 6))
        bar.grid(row=4, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)

        ttk.Label(bar, text="Filter", style="Small.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", self._on_filter_changed)
        filt = tk.Entry(
            bar, textvariable=self.filter_var, bg=CARD, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, font=SMALL_FONT,
        )
        filt.grid(row=0, column=1, sticky="ew", ipady=6)
        filt.bind("<Escape>", lambda _e: self.filter_var.set(""))

        ttk.Button(bar, text="All", style="Ghost.TButton",
                   command=self._select_all).grid(row=0, column=2, padx=(10, 0))
        ttk.Button(bar, text="None", style="Ghost.TButton",
                   command=self._select_none).grid(row=0, column=3)
        ttk.Button(bar, text="Invert", style="Ghost.TButton",
                   command=self._select_invert).grid(row=0, column=4)
        self.info_label = ttk.Label(bar, text="", style="Small.TLabel")
        self.info_label.grid(row=0, column=5, padx=(14, 0))

    def _build_tree(self) -> None:
        wrap = ttk.Frame(self, padding=(20, 0, 20, 0))
        wrap.grid(row=5, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        columns = ("sel", "name", "size", "kind")
        self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("sel", text="")
        self.tree.heading("name", text="Name", anchor="w", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Size", anchor="e", command=lambda: self._sort_by("size"))
        self.tree.heading("kind", text="Type", anchor="w", command=lambda: self._sort_by("kind"))
        self.tree.column("sel", width=38, minwidth=38, stretch=False, anchor="center")
        self.tree.column("name", width=700, minwidth=260, stretch=True)
        self.tree.column("size", width=110, minwidth=90, stretch=False, anchor="e")
        self.tree.column("kind", width=100, minwidth=70, stretch=False, anchor="w")
        self.tree.tag_configure("dir", foreground=ACCENT)
        self.tree.tag_configure("checked", background=CHECKED_BG)
        self.tree.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(
            wrap, orient="vertical", command=self.tree.yview,
            bg=CARD, troughcolor=PANEL, activebackground=CARD_HI,
            relief="flat", bd=0, highlightthickness=0, width=12,
            elementborderwidth=0,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<BackSpace>", lambda _e: self._go_up())
        self.tree.bind("<Control-a>", lambda _e: (self._select_all(), "break")[1])
        self.tree.bind("<F5>", lambda _e: self._refresh())
        self.tree.bind("<Button-3>", self._on_right_click)

        self.menu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT,
                            activebackground=ACCENT_DARK, activeforeground=TEXT,
                            bd=0, font=SMALL_FONT)
        self.menu.add_command(label="Open folder", command=self._menu_open)
        self.menu.add_command(label="Select / deselect", command=self._menu_toggle)
        self.menu.add_separator()
        self.menu.add_command(label="Copy index URL", command=self._menu_copy_index)
        self.menu.add_command(label="Copy proxy link", command=self._menu_copy_proxy)
        self.menu.add_command(label="Send to IDM", command=self._menu_send)

    def _build_actions(self) -> None:
        bar = ttk.Frame(self, padding=(20, 10, 20, 0))
        bar.grid(row=6, column=0, sticky="ew")

        self.send_btn = ttk.Button(bar, text="⬇  Send to IDM", style="Accent.TButton",
                                   command=lambda: self._start_download(False))
        self.send_btn.pack(side="left")
        self.queue_btn = ttk.Button(bar, text="＋  Add to IDM queue",
                                    command=lambda: self._start_download(True))
        self.queue_btn.pack(side="left", padx=(8, 0))
        self._buttons.extend([self.send_btn, self.queue_btn])

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=14, pady=2)

        self.copy_btn = ttk.Button(bar, text="Copy links", command=self._copy_links)
        self.copy_btn.pack(side="left")
        self.copy_urls_btn = ttk.Button(bar, text="Copy index URLs", command=self._copy_index_urls)
        self.copy_urls_btn.pack(side="left", padx=(8, 0))
        self.export_btn = ttk.Button(bar, text="Export…", command=self._export_links)
        self.export_btn.pack(side="left", padx=(8, 0))
        self.open_idm_btn = ttk.Button(bar, text="Open IDM", command=self._open_idm)
        self.open_idm_btn.pack(side="left", padx=(14, 0))
        self._buttons.extend([self.copy_btn, self.copy_urls_btn, self.export_btn, self.open_idm_btn])

        self.progress = ttk.Progressbar(bar, orient="horizontal", length=190, mode="determinate")
        self.progress.pack(side="right")

    def _build_status(self) -> None:
        bar = ttk.Frame(self, padding=(20, 8, 20, 10))
        bar.grid(row=7, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status_var, style="Small.TLabel").grid(
            row=0, column=0, sticky="w")
        self.log_btn = ttk.Button(bar, text="Show log", style="Ghost.TButton",
                                  command=self._toggle_log)
        self.log_btn.grid(row=0, column=1, sticky="e")

    def _build_log(self) -> None:
        self.log_frame = ttk.Frame(self, padding=(20, 0, 20, 12))
        self.log_frame.grid(row=8, column=0, sticky="ew")
        self.log_frame.grid_columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            self.log_frame, height=8, bg="#0b0e13", fg=MUTED, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            font=MONO_FONT, wrap="word", padx=10, pady=8,
        )
        self.log_text.grid(row=0, column=0, sticky="ew")
        self.log_text.configure(state="disabled")
        log_scroll = tk.Scrollbar(
            self.log_frame, orient="vertical", command=self.log_text.yview,
            bg=CARD, troughcolor="#0b0e13", activebackground=CARD_HI,
            relief="flat", bd=0, highlightthickness=0, width=12,
            elementborderwidth=0,
        )
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_frame.grid_remove()

    # -------------------------------------------------------------- ui plumbing
    def queue_log(self, message: str) -> None:
        logging.info(message)
        self.queue.put(("log", message))

    log = queue_log

    def set_status(self, message: str) -> None:
        self.queue.put(("status", message))

    def set_progress(self, done: int, total: int) -> None:
        self.queue.put(("progress", (done, total)))

    def ui_call(self, func):
        if threading.current_thread() is threading.main_thread():
            return func()
        event = threading.Event()
        box: dict = {}

        def wrapper() -> None:
            try:
                box["value"] = func()
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                event.set()

        self.queue.put(("call", wrapper))
        event.wait()
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "progress":
                    done, total = payload
                    self.progress.configure(maximum=max(total, 1), value=done)
                elif kind == "call":
                    payload()
                elif kind == "listing":
                    self._on_listing(*payload)
                elif kind == "busy":
                    self._apply_busy(payload)
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            logging.exception("UI queue error")
        self.after(70, self._drain)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 1500:
            self.log_text.delete("1.0", f"{lines - 1000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _apply_busy(self, busy: bool) -> None:
        self._busy = busy
        for btn in self._buttons:
            btn.configure(state="disabled" if busy else "normal")
        if not busy:
            self.progress.configure(value=0)

    def run_task(self, func) -> None:
        if self._busy:
            messagebox.showinfo("Busy", "Another task is still running.", parent=self)
            return
        self.queue.put(("busy", True))

        def runner() -> None:
            try:
                func()
            except Exception as exc:  # noqa: BLE001
                logging.error("Task failed: %s\n%s", exc, traceback.format_exc())
                self.log(f"ERROR: {exc}")
                self.set_status("Error – see log")
                self.ui_call(lambda: messagebox.showerror("Error", str(exc), parent=self))
            finally:
                self.queue.put(("busy", False))

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------- state
    def _initial_load(self) -> None:
        path = self.settings.last_path or "/"
        try:
            self.navigate(path)
        except ValueError:
            self.navigate("/")

    def navigate(self, path: str, record: bool = True) -> None:
        try:
            path = normalize_path(path)
        except ValueError as exc:
            messagebox.showerror("Invalid path", str(exc), parent=self)
            return
        if record:
            if self.hist_pos >= 0 and self.history[self.hist_pos] == path:
                pass
            else:
                self.history = self.history[:self.hist_pos + 1] + [path]
                self.hist_pos = len(self.history) - 1
        self.current_path = path
        self.path_var.set(unquote(path))
        self.section_var.set(path.split("/")[1] if path != "/" else "Root")
        self._update_nav_buttons()
        self._update_breadcrumb()
        self._load(path, refresh=False)

    def _load(self, path: str, refresh: bool) -> None:
        token = object()
        self._load_token = token
        self.entries = []
        self.tree.delete(*self.tree.get_children())
        self.set_status(f"Loading {unquote(path)}…")
        self.queue.put(("busy", True))

        def worker() -> None:
            try:
                entries = self.index.list_dir(path, refresh=refresh)
                self.queue.put(("listing", (token, path, entries, None)))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("listing", (token, path, [], exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_listing(self, token, path: str, entries: list[Entry], error: Exception | None) -> None:
        if token is not self._load_token:
            return
        self.queue.put(("busy", False))
        if error is not None:
            if self._initialising and path != "/":
                self._initialising = False
                self.navigate("/")
                return
            self._initialising = False
            self.log(f"Could not load {unquote(path)}: {error}")
            self.set_status(str(error))
            self._render()
            messagebox.showerror("Could not load folder", str(error), parent=self)
            return

        self._initialising = False
        self.entries = entries
        for entry in entries:
            if not entry.is_dir:
                self.sizes[entry.path] = entry.size
        if path == "/":
            sections = [e.name for e in entries if e.is_dir]
            if sections:
                self.section_box.configure(values=["Root", *sections])
                self.section_box.set("Root")
        self.settings.last_path = path
        try:
            self.settings.save()
        except OSError:
            pass
        self._render()
        self.set_status(f"{unquote(path)}  ·  {len(entries)} items")

    def _update_nav_buttons(self) -> None:
        self.back_btn.configure(state="normal" if self.hist_pos > 0 else "disabled")
        self.fwd_btn.configure(
            state="normal" if 0 <= self.hist_pos < len(self.history) - 1 else "disabled")

    def _update_breadcrumb(self) -> None:
        for child in self.crumb.winfo_children():
            child.destroy()
        segments = [("Root", "/")] + path_segments(self.current_path)
        for index, (label, path) in enumerate(segments):
            if index:
                tk.Label(self.crumb, text="›", bg=BG, fg=DIM, font=SMALL_FONT).pack(
                    side="left", padx=2)
            active = index == len(segments) - 1
            item = tk.Label(
                self.crumb, text=label, bg=BG,
                fg=TEXT if active else ACCENT,
                font=UI_BOLD if active else SMALL_FONT,
                cursor="hand2" if not active else "arrow",
            )
            if not active:
                item.bind("<Button-1>", lambda _e, p=path: self.navigate(p))
            item.pack(side="left")

    def _on_section(self, _event=None) -> None:
        section = self.section_var.get()
        if not section:
            return
        if section == "Root":
            self.navigate("/")
        else:
            self.navigate(f"/{section}/")

    def _go_typed(self) -> None:
        self.navigate(self.path_var.get())

    def _go_up(self) -> None:
        self.navigate(parent_path(self.current_path))

    def _go_back(self) -> None:
        if self.hist_pos > 0:
            self.hist_pos -= 1
            self.navigate(self.history[self.hist_pos], record=False)

    def _go_forward(self) -> None:
        if self.hist_pos < len(self.history) - 1:
            self.hist_pos += 1
            self.navigate(self.history[self.hist_pos], record=False)

    def _refresh(self) -> None:
        self.index._cache.pop(self.current_path, None)
        self._load(self.current_path, refresh=True)

    # ------------------------------------------------------------------ render
    def _sort_by(self, column: str) -> None:
        if self.sort_col == column:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = column
            self.sort_desc = False
        self._render()

    def _on_filter_changed(self, *_args) -> None:
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(220, self._render)

    def _visible_entries(self) -> list[Entry]:
        query = self.filter_var.get().strip().lower()
        rows = [e for e in self.entries if not query or query in e.name.lower()]

        if self.sort_col == "name":
            key = lambda e: natural_key(e.name)  # noqa: E731
        elif self.sort_col == "size":
            key = lambda e: (e.size, natural_key(e.name))  # noqa: E731
        else:
            key = lambda e: (e.kind.lower(), natural_key(e.name))  # noqa: E731

        dirs = sorted([e for e in rows if e.is_dir], key=key, reverse=self.sort_desc)
        files = sorted([e for e in rows if not e.is_dir], key=key, reverse=self.sort_desc)
        return dirs + files

    def _render(self) -> None:
        self._filter_job = None
        self._render_gen += 1
        generation = self._render_gen
        if self._render_after:
            try:
                self.after_cancel(self._render_after)
            except tk.TclError:
                pass
            self._render_after = None

        self._render_rows = self._visible_entries()
        self._render_pos = 0
        self.tree.delete(*self.tree.get_children())
        self._render_chunk(generation)

    def _render_chunk(self, generation: int) -> None:
        if generation != self._render_gen:
            return
        rows = self._render_rows
        start = self._render_pos
        end = min(start + 600, len(rows))
        for entry in rows[start:end]:
            checked = entry.path in self.selection
            tags: list[str] = []
            if entry.is_dir:
                tags.append("dir")
            if checked:
                tags.append("checked")
            self.tree.insert(
                "", "end", iid=entry.path,
                values=("☑" if checked else "☐", entry.name,
                        human_size(entry.size), entry.kind),
                tags=tuple(tags),
            )
        self._render_pos = end

        if end < len(rows):
            if len(rows) > 1200:
                self.set_status(f"Rendering {end:,} of {len(rows):,} items…")
            self._render_after = self.after(1, lambda: self._render_chunk(generation))
            return

        self._render_after = None
        if rows:
            self.tree.yview_moveto(0)
        self._update_info()
        self.set_status(f"{unquote(self.current_path)}  ·  {len(self.entries)} items")

    def _update_info(self) -> None:
        selected = len(self.selection)
        known = sum(self.sizes.get(path, 0) for path in self.selection)
        parts = [f"{len(self.entries)} items"]
        if self.filter_var.get().strip():
            parts.append(f"{len(self.tree.get_children())} shown")
        parts.append(f"{selected} selected")
        if known:
            parts.append(human_size(known))
        self.info_label.configure(text="  ·  ".join(parts))

    # ---------------------------------------------------------------- selection
    def _toggle(self, path: str) -> None:
        if path in self.selection:
            self.selection.remove(path)
        else:
            self.selection.add(path)
        if self.tree.exists(path):
            checked = path in self.selection
            values = list(self.tree.item(path, "values"))
            values[0] = "☑" if checked else "☐"
            entry = next((e for e in self.entries if e.path == path), None)
            tags: list[str] = []
            if entry and entry.is_dir:
                tags.append("dir")
            if checked:
                tags.append("checked")
            self.tree.item(path, values=values, tags=tuple(tags))
        self._update_info()

    def _row_at(self, event) -> str:
        row = self.tree.identify_row(event.y)
        return row or ""

    def _on_tree_click(self, event):
        column = self.tree.identify_column(event.x)
        row = self._row_at(event)
        if not row:
            return None
        if column == "#1":
            self._toggle(row)
            self.tree.focus(row)
            return "break"
        return None

    def _on_tree_double(self, event):
        column = self.tree.identify_column(event.x)
        row = self._row_at(event)
        if not row:
            return None
        if column == "#1":
            return "break"
        entry = next((e for e in self.entries if e.path == row), None)
        if entry and entry.is_dir:
            self.navigate(entry.path)
            return "break"
        self._toggle(row)
        return "break"

    def _on_space(self, _event):
        row = self.tree.focus() or (self.tree.selection()[0] if self.tree.selection() else "")
        if row:
            self._toggle(row)
        return "break"

    def _on_return(self, _event):
        row = self.tree.focus() or (self.tree.selection()[0] if self.tree.selection() else "")
        entry = next((e for e in self.entries if e.path == row), None)
        if entry and entry.is_dir:
            self.navigate(entry.path)
        elif row:
            self._toggle(row)
        return "break"

    def _select_all(self) -> None:
        for entry in self._visible_entries():
            self.selection.add(entry.path)
        self._render()

    def _select_none(self) -> None:
        self.selection.clear()
        self._render()

    def _select_invert(self) -> None:
        for entry in self._visible_entries():
            if entry.path in self.selection:
                self.selection.remove(entry.path)
            else:
                self.selection.add(entry.path)
        self._render()

    def _on_right_click(self, event):
        row = self._row_at(event)
        if not row:
            return
        self.tree.focus(row)
        entry = next((e for e in self.entries if e.path == row), None)
        is_dir = bool(entry and entry.is_dir)
        self.menu.entryconfigure(0, state="normal" if is_dir else "disabled")
        self.menu.entryconfigure(1, label="Deselect" if row in self.selection else "Select")
        self.menu.entryconfigure(3, state="disabled" if is_dir else "normal")
        self.menu.entryconfigure(4, state="disabled" if is_dir else "normal")
        self.menu.entryconfigure(5, state="normal")
        self.menu.tk_popup(event.x_root, event.y_root)

    def _menu_row(self) -> str:
        return self.tree.focus()

    def _menu_open(self) -> None:
        row = self._menu_row()
        entry = next((e for e in self.entries if e.path == row), None)
        if entry and entry.is_dir:
            self.navigate(entry.path)

    def _menu_toggle(self) -> None:
        row = self._menu_row()
        if row:
            self._toggle(row)

    def _menu_copy_index(self) -> None:
        row = self._menu_row()
        if row:
            self._set_clipboard(BASE_URL + row)
            self.set_status("Index URL copied")

    def _menu_copy_proxy(self) -> None:
        row = self._menu_row()
        if row:
            self.run_task(lambda: self._copy_job([row]))

    def _menu_send(self) -> None:
        row = self._menu_row()
        if row:
            if row not in self.selection:
                self._toggle(row)
            self._start_download(self.settings.queue_only)

    # -------------------------------------------------------------- collection
    def _collect_files(self, paths: list[str]) -> list[Entry]:
        known = {e.path: e for e in self.entries}
        files: dict[str, Entry] = {}
        folders: list[str] = []

        for path in paths:
            if path in known:
                entry = known[path]
                if entry.is_dir:
                    folders.append(path)
                else:
                    files[path] = entry
            elif path.endswith("/"):
                folders.append(path)
            else:
                name = unquote(path.rstrip("/").split("/")[-1])
                dot = name.rfind(".")
                files[path] = Entry(name=name, path=path, is_dir=False,
                                    size=self.sizes.get(path, -1),
                                    kind=name[dot + 1:].upper() if dot > 0 else "File")

        seen_dirs: set[str] = set()
        queue_dirs = list(folders)
        cap = self.settings.max_crawl_dirs
        while queue_dirs:
            folder = queue_dirs.pop(0)
            if folder in seen_dirs:
                continue
            if len(seen_dirs) >= cap:
                self.log(f"Folder limit reached ({cap}); some folders were not scanned.")
                break
            seen_dirs.add(folder)
            self.set_status(
                f"Scanning {unquote(folder)}  ·  {len(seen_dirs)} folders  ·  {len(files)} files")
            try:
                entries = self.index.list_dir(folder)
            except IndexError_ as exc:
                self.log(f"Skip {unquote(folder)}: {exc}")
                continue
            for entry in entries:
                if entry.is_dir:
                    queue_dirs.append(entry.path)
                else:
                    files.setdefault(entry.path, entry)
                    self.sizes[entry.path] = entry.size

        out = list(files.values())
        if self.settings.only_media:
            allowed = {e.lower() for e in self.settings.media_exts}
            out = [e for e in out if Path(e.name).suffix.lower() in allowed]
        out.sort(key=lambda e: natural_key(e.path))
        self.log(f"Collected {len(out)} file(s) from {len(paths)} selection(s).")
        return out

    def _target(self, entry: Entry) -> tuple[str, str]:
        base = Path(self.settings.download_dir).expanduser()
        parts = [p for p in entry.path.split("/") if p]
        # parts[0] is the section (kdrama, movies, …); last part is the filename.
        folders = parts[1:-1] if len(parts) > 1 else []
        folder = base
        if self.settings.preserve_folders and folders:
            folder = base.joinpath(*[safe_name(unquote(p)) for p in folders])
        return str(folder), safe_name(entry.name)

    def _dest_exists(self, entry: Entry) -> bool:
        folder, name = self._target(entry)
        return (Path(folder) / name).exists()

    # ----------------------------------------------------------------- actions
    def _start_download(self, queue_only: bool) -> None:
        if not self.selection:
            messagebox.showinfo("Nothing selected", "Tick a few files or folders first.", parent=self)
            return
        if not self.idm.available:
            if messagebox.askyesno(
                "IDM not found",
                "Internet Download Manager was not found.\n\nLocate IDMan.exe now?",
                parent=self,
            ):
                self._open_settings()
            return

        paths = sorted(self.selection)
        self.run_task(lambda: self._download_job(paths, queue_only))

    def _download_job(self, paths: list[str], queue_only: bool) -> None:
        files = self._collect_files(paths)
        if not files:
            self.ui_call(lambda: messagebox.showinfo(
                "Nothing to download", "No matching files were found.", parent=self))
            return

        skipped_existing = []
        if self.settings.skip_existing:
            remaining = []
            for entry in files:
                try:
                    if self._dest_exists(entry):
                        skipped_existing.append(entry)
                        continue
                except OSError:
                    pass
                remaining.append(entry)
            files = remaining

        if not files:
            self.log(f"All {len(skipped_existing)} file(s) already exist locally.")
            self.ui_call(lambda: messagebox.showinfo(
                "Already downloaded",
                f"All {len(skipped_existing)} selected file(s) already exist in the download folder.",
                parent=self))
            return

        total_size = sum(e.size for e in files if e.size and e.size > 0)
        lines = [f"{len(files)} file(s)", f"{human_size(total_size)} total"]
        if skipped_existing:
            lines.append(f"{len(skipped_existing)} already on disk (skipped)")
        lines.append("")
        if queue_only:
            lines.append("They are added to the IDM queue (nothing starts automatically).")
        else:
            lines.append("IDM starts downloading immediately.")
        if len(files) > 2:
            lines.append("")
            lines.append("The proxy allows only 2 simultaneous downloads – the queue is safer.")
        message = "Send to IDM?\n\n" + "\n".join(lines)
        if not self.ui_call(lambda: messagebox.askyesno("Send to IDM", message, parent=self)):
            self.set_status("Cancelled")
            return

        self.set_status(f"Minting {len(files)} proxy link(s)…")
        links, errors = self.resolver.mint(
            [e.path for e in files], progress=self.set_progress)

        if not links:
            detail = ""
            if errors:
                detail = f"\n\n{len(errors)} link(s) could not be created."
            raise RuntimeError(
                "No proxy links could be created." + detail +
                "\n\nTry Settings → Verify proxy session."
            )

        sent = 0
        failed = 0
        for entry in files:
            link = links.get(entry.path)
            if not link:
                failed += 1
                continue
            folder, filename = self._target(entry)
            try:
                self.idm.send(link, folder, filename, queue_only)
                sent += 1
                self.log(f"IDM ← {filename}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.log(f"IDM failed for {filename}: {exc}")
            time.sleep(0.18)

        skipped = len(skipped_existing)
        self.log(f"Sent {sent} file(s) to IDM ({skipped} skipped, {failed} failed).")
        self.set_status(f"Sent {sent} to IDM")
        summary = f"{sent} file(s) sent to IDM."
        if skipped:
            summary += f"\n{skipped} skipped (already downloaded)."
        if failed:
            summary += f"\n{failed} failed – see the log."
        if queue_only:
            summary += "\n\nOpen IDM and start the queue when you are ready."
        self.ui_call(lambda: messagebox.showinfo("IDM", summary, parent=self))

    def _copy_job(self, paths: list[str]) -> None:
        files = self._collect_files(paths)
        if not files:
            self.ui_call(lambda: messagebox.showinfo(
                "Nothing selected", "No files found for the selection.", parent=self))
            return
        links, errors = self.resolver.mint([e.path for e in files], progress=self.set_progress)
        urls = [links[e.path] for e in files if e.path in links]
        if not urls:
            raise RuntimeError("No links could be created. Try Settings → Verify proxy session.")
        self.ui_call(lambda: self._set_clipboard("\n".join(urls)))
        self.log(f"Copied {len(urls)} proxy link(s) to the clipboard.")
        self.set_status(f"Copied {len(urls)} link(s)")
        if errors:
            self.ui_call(lambda: messagebox.showwarning(
                "Partial result", f"Copied {len(urls)} link(s); {len(errors)} failed.", parent=self))

    def _copy_links(self) -> None:
        if not self.selection:
            messagebox.showinfo("Nothing selected", "Tick some files first.", parent=self)
            return
        paths = sorted(self.selection)
        self.run_task(lambda: self._copy_job(paths))

    def _copy_index_urls(self) -> None:
        if not self.selection:
            messagebox.showinfo("Nothing selected", "Tick some files first.", parent=self)
            return
        urls = [BASE_URL + path for path in sorted(self.selection)]
        if not urls:
            return
        self._set_clipboard("\n".join(urls))
        self.set_status(f"Copied {len(urls)} index URL(s)")
        self.log(f"Copied {len(urls)} index URL(s).")

    def _open_idm(self) -> None:
        try:
            self.idm.open()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("IDM", str(exc), parent=self)

    def _export_links(self) -> None:
        if not self.selection:
            messagebox.showinfo("Nothing selected", "Tick some files first.", parent=self)
            return
        target = filedialog.asksaveasfilename(
            parent=self, title="Export proxy links", defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            initialfile="111477-links.txt",
        )
        if not target:
            return
        paths = sorted(self.selection)

        def job() -> None:
            files = self._collect_files(paths)
            if not files:
                return
            links, errors = self.resolver.mint([e.path for e in files], progress=self.set_progress)
            lines = [links[e.path] for e in files if e.path in links]
            Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.log(f"Exported {len(lines)} link(s) to {target}")
            self.set_status(f"Exported {len(lines)} link(s)")
            message = f"Exported {len(lines)} link(s)."
            if errors:
                message += f"\n{len(errors)} link(s) failed."
            self.ui_call(lambda: messagebox.showinfo("Export", message, parent=self))

        self.run_task(job)

    # --------------------------------------------------------------- clipboard
    def _set_clipboard(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()

    # ---------------------------------------------------------------- settings
    def _open_settings(self) -> None:
        SettingsDialog(self)

    def _verify_session(self) -> None:
        self.resolver.forget_session()

        def job() -> None:
            self.resolver.warm_up("/", force=True)
            self.log("Proxy session verified successfully.")
            self.set_status("Proxy session verified")
            self.ui_call(lambda: messagebox.showinfo(
                "Proxy session", "Cloudflare verification is valid again.", parent=self))

        self.run_task(job)

    def _check_idm(self) -> None:
        if not self.idm.available:
            detected = find_idm()
            if detected:
                self.idm.path = detected
                self.settings.idm_path = detected
            else:
                self.idm_badge.configure(text="IDM not found", bg="#3a2020", fg=RED)
                self._show_banner(
                    "Internet Download Manager was not found. Set the IDMan.exe path in Settings.",
                    [("Open settings", self._open_settings)],
                )
                return
        conn = self.idm.connections()
        label = "IDM ready" + (f"  ·  {conn} conn" if conn else "")
        self.idm_badge.configure(
            text=label, bg="#16301f" if not conn or conn <= 1 else "#332a16",
            fg=GREEN if not conn or conn <= 1 else AMBER,
        )
        if conn and conn > 1:
            self._show_banner(
                f"IDM is set to {conn} connections per file, but these proxy links allow exactly 1. "
                "Downloads will stall or waste link uses. Set it to 1 while using this app.",
                [("Set to 1", self._fix_connections), ("Dismiss", self._hide_banner)],
            )
        else:
            self._hide_banner()

    def _fix_connections(self) -> None:
        previous = self.idm.connections()
        try:
            self.idm.set_connections(1)
        except OSError as exc:
            messagebox.showerror("IDM settings", f"Could not update the registry:\n{exc}", parent=self)
            return
        if previous and previous != 1:
            self.settings.idm_prev_connections = previous
            self.settings.save()
        self.log(f"IDM max connections changed from {previous} to 1 (restart IDM to apply).")
        self._show_banner(
            "IDM is now set to 1 connection. Fully exit IDM (tray icon → Exit) and reopen it "
            "for the change to take effect.",
            [("Undo", self._undo_connections), ("Dismiss", self._hide_banner)],
        )
        self._check_idm_badge()

    def _undo_connections(self) -> None:
        previous = self.settings.idm_prev_connections
        if previous:
            try:
                self.idm.set_connections(previous)
                self.log(f"IDM max connections restored to {previous}.")
            except OSError as exc:
                messagebox.showerror("IDM settings", str(exc), parent=self)
        self._check_idm()

    def _check_idm_badge(self) -> None:
        conn = self.idm.connections()
        self.idm_badge.configure(
            text="IDM ready" + (f"  ·  {conn} conn" if conn else ""),
            bg="#16301f" if not conn or conn <= 1 else "#332a16",
            fg=GREEN if not conn or conn <= 1 else AMBER,
        )

    def _show_banner(self, text: str, actions: list[tuple[str, object]]) -> None:
        self.banner_label.configure(text=text, wraplength=max(self.winfo_width() - 420, 480))
        for child in self.banner_actions.winfo_children():
            child.destroy()
        for label, command in actions:
            tk.Button(
                self.banner_actions, text=label, command=command, bg="#3a2f16",
                fg="#ffd98a", activebackground="#4a3c1c", activeforeground="#ffe6a8",
                relief="flat", font=SMALL_FONT, padx=10, pady=3, cursor="hand2",
                bd=0, highlightthickness=0,
            ).pack(side="left", padx=(6, 0))
        self.banner.grid()

    def _hide_banner(self) -> None:
        self.banner.grid_remove()

    def _toggle_log(self) -> None:
        if self.log_frame.winfo_viewable():
            self.log_frame.grid_remove()
            self.log_btn.configure(text="Show log")
        else:
            self.log_frame.grid()
            self.log_btn.configure(text="Hide log")
            self._append_log("Log opened")

    # -------------------------------------------------------------------- help
    def _show_help(self) -> None:
        help_box = tk.Toplevel(self)
        help_box.title("How it works")
        help_box.configure(bg=BG)
        help_box.transient(self)
        help_box.resizable(False, False)

        frame = ttk.Frame(help_box, padding=22)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="How this works", style="Title.TLabel").pack(anchor="w")
        text = (
            "1.  Browse the index just like a file manager – double-click folders, use the "
            "filter box, tick episodes.\n\n"
            "2.  Press “Send to IDM”. Each selected file is turned into a temporary proxy link "
            "(p.111477.xyz) and handed to Internet Download Manager.\n\n"
            "3.  The first time (and whenever the session expires) a Chrome window opens so "
            "Cloudflare can verify you. Click the checkbox if one appears, then it closes "
            "by itself.\n\n"
            "Important limits of the proxy:\n"
            "•  Proxy links allow exactly ONE connection per file – set IDM's default "
            "connections to 1 (the app offers a one-click fix).\n"
            "•  Only 2 files can download at the same time. Use “Add to IDM queue”.\n"
            "•  Links live for 24 hours and are locked to your IP; the site also has a daily "
            "download quota (about 30 files, slowly refilling).\n"
            "•  Copy links instead of downloading if you want to use wget/curl.\n\n"
            "Tip: right-click a row for copy/send shortcuts."
        )
        ttk.Label(frame, text=text, style="Muted.TLabel", justify="left", wraplength=560).pack(
            anchor="w", pady=(10, 16))
        ttk.Button(frame, text="Got it", command=help_box.destroy).pack(anchor="e")
        help_box.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - help_box.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - help_box.winfo_height()) // 3
        help_box.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # -------------------------------------------------------------------- close
    def report_callback_exception(self, exc, val, tb):  # noqa: D401 - Tk hook
        logging.error("UI callback failed: %s", val, exc_info=(exc, val, tb))
        try:
            messagebox.showerror("Unexpected error", str(val), parent=self)
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self) -> None:
        try:
            self.settings.last_path = self.current_path
            self.settings.save()
        except OSError:
            pass
        self.store.close()
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.configure(bg=BG)
        self.transient(app)
        self.grab_set()
        self.resizable(False, False)

        s = app.settings
        self.dir_var = tk.StringVar(value=s.download_dir)
        self.idm_var = tk.StringVar(value=app.idm.path)
        self.queue_var = tk.BooleanVar(value=s.queue_only)
        self.preserve_var = tk.BooleanVar(value=s.preserve_folders)
        self.skip_var = tk.BooleanVar(value=s.skip_existing)
        self.media_var = tk.BooleanVar(value=s.only_media)
        self.exts_var = tk.StringVar(value=" ".join(s.media_exts))

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(1, weight=1)

        row = 0
        ttk.Label(body, text="Download folder", style="Small.TLabel").grid(
            row=row, column=0, sticky="w", pady=(0, 4))
        ttk.Label(body, text="Everything IDM fetches is saved here (series folders are kept).",
                  style="Small.TLabel", foreground=DIM).grid(
            row=row, column=1, sticky="w", padx=(12, 0), pady=(0, 4))
        row += 1
        dir_bar = ttk.Frame(body)
        dir_bar.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        dir_bar.grid_columnconfigure(0, weight=1)
        tk.Entry(dir_bar, textvariable=self.dir_var, bg=CARD, fg=TEXT,
                 insertbackground=TEXT, relief="flat", highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT).grid(
            row=0, column=0, sticky="ew", ipady=6)
        ttk.Button(dir_bar, text="Browse…", command=self._browse_dir).grid(
            row=0, column=1, padx=(8, 0))
        row += 1

        self._row(body, row, "IDM path", self.idm_var, browse=self._browse_idm, auto=self._auto_idm)
        row += 2

        ttk.Checkbutton(body, text="Add downloads to the IDM queue instead of starting them",
                        variable=self.queue_var).grid(row=row, column=0, columnspan=2,
                                                      sticky="w", pady=2)
        row += 1
        ttk.Checkbutton(body, text="Keep series / movie folder structure",
                        variable=self.preserve_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Checkbutton(body, text="Skip files that already exist in the download folder",
                        variable=self.skip_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Checkbutton(body, text="Only download media / subtitle files when expanding folders",
                        variable=self.media_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Label(body, text="Extensions", style="Small.TLabel").grid(row=row, column=0, sticky="w")
        exts = tk.Entry(body, textvariable=self.exts_var, bg=CARD, fg=TEXT,
                        insertbackground=TEXT, relief="flat", highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=ACCENT)
        exts.grid(row=row, column=1, sticky="ew", padx=(12, 0), ipady=5)
        row += 1

        sep = ttk.Separator(body, orient="horizontal")
        sep.grid(row=row, column=0, columnspan=2, sticky="ew", pady=14)
        row += 1

        connections = app.idm.connections()
        ttk.Label(body, text="IDM connections", style="Small.TLabel").grid(
            row=row, column=0, sticky="w")
        self.conn_label = ttk.Label(
            body,
            text=(f"Currently {connections} per file. Proxy links need exactly 1."
                  if connections else "Registry value not found."),
            style="Muted.TLabel", font=SMALL_FONT)
        self.conn_label.grid(row=row, column=1, sticky="w", padx=(12, 0))
        row += 1
        conn_buttons = ttk.Frame(body)
        conn_buttons.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(conn_buttons, text="Set IDM to 1 connection",
                   command=self._set_conn).pack(side="left")
        if s.idm_prev_connections:
            ttk.Button(conn_buttons, text=f"Restore {s.idm_prev_connections}",
                       command=self._restore_conn).pack(side="left", padx=(8, 0))
        row += 1

        sep2 = ttk.Separator(body, orient="horizontal")
        sep2.grid(row=row, column=0, columnspan=2, sticky="ew", pady=14)
        row += 1

        status = app.resolver.session_age_minutes
        session_text = (f"Cloudflare session active ({status:.0f} min old)."
                        if status is not None else "No Cloudflare session yet.")
        ttk.Label(body, text=session_text, style="Muted.TLabel", font=SMALL_FONT).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        session_buttons = ttk.Frame(body)
        session_buttons.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(session_buttons, text="Verify proxy session now",
                   command=self._verify).pack(side="left")
        ttk.Button(session_buttons, text="Forget session",
                   command=self._forget).pack(side="left", padx=(8, 0))
        ttk.Button(session_buttons, text="Reset browser profile",
                   command=self._reset_profile).pack(side="left", padx=(8, 0))
        row += 1

        ttk.Button(body, text="Open data folder", style="Ghost.TButton",
                   command=lambda: os.startfile(DATA_DIR)).grid(
            row=row, column=0, sticky="w", pady=(14, 0))
        ttk.Button(body, text="Clear index cache", style="Ghost.TButton",
                   command=self._clear_cache).grid(row=row, column=1, sticky="w",
                                                   padx=(12, 0), pady=(14, 0))
        row += 1

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Save", style="Accent.TButton",
                   command=self._save).pack(side="left")

        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + 60
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.bind("<Escape>", lambda _e: self.destroy())

    # ------------------------------------------------------------------ widgets
    def _row(self, parent, row: int, label: str, variable, browse=None, auto=None) -> None:
        ttk.Label(parent, text=label, style="Small.TLabel").grid(
            row=row, column=0, sticky="w", pady=(4, 2))
        entry = tk.Entry(parent, textvariable=variable, bg=CARD, fg=TEXT,
                         insertbackground=TEXT, relief="flat", highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
        entry.grid(row=row + 0, column=1, sticky="ew", padx=(12, 0), ipady=5)
        if browse or auto:
            bar = ttk.Frame(parent)
            bar.grid(row=row + 1, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
            if browse:
                ttk.Button(bar, text="Browse…", command=browse).pack(side="left")
            if auto:
                ttk.Button(bar, text="Auto-detect", command=auto).pack(side="left", padx=(6, 0))

    # ------------------------------------------------------------------ actions
    def _browse_dir(self) -> None:
        chosen = filedialog.askdirectory(parent=self, initialdir=self.dir_var.get() or None)
        if chosen:
            self.dir_var.set(chosen)

    def _browse_idm(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self, title="Select IDMan.exe", filetypes=[("IDMan.exe", "IDMan.exe"), ("Programs", "*.exe")])
        if chosen:
            self.idm_var.set(chosen)

    def _auto_idm(self) -> None:
        found = find_idm()
        if found:
            self.idm_var.set(found)
        else:
            messagebox.showwarning("IDM", "Could not find IDMan.exe automatically.", parent=self)

    def _set_conn(self) -> None:
        app = self.app
        previous = app.idm.connections()
        try:
            app.idm.set_connections(1)
        except OSError as exc:
            messagebox.showerror("IDM", str(exc), parent=self)
            return
        if previous and previous != 1:
            app.settings.idm_prev_connections = previous
            app.settings.save()
        self.conn_label.configure(text="Set to 1. Restart IDM for it to take effect.")
        app.log(f"IDM connections set to 1 (was {previous}).")
        app._check_idm_badge()
        if app.idm.connections() == 1:
            app._hide_banner()

    def _restore_conn(self) -> None:
        app = self.app
        previous = app.settings.idm_prev_connections
        if not previous:
            return
        try:
            app.idm.set_connections(previous)
        except OSError as exc:
            messagebox.showerror("IDM", str(exc), parent=self)
            return
        self.conn_label.configure(text=f"Restored to {previous}. Restart IDM to apply.")
        app.log(f"IDM connections restored to {previous}.")
        app._check_idm_badge()

    def _verify(self) -> None:
        self.destroy()
        self.app._verify_session()

    def _forget(self) -> None:
        self.app.resolver.forget_session()
        self.app.log("Proxy session forgotten.")
        messagebox.showinfo("Session", "The stored Cloudflare session was removed.", parent=self)

    def _reset_profile(self) -> None:
        if messagebox.askyesno(
            "Reset browser profile",
            "This clears the verification browser profile. The next download will need a "
            "fresh Cloudflare check.\n\nContinue?",
            parent=self,
        ):
            self.app.resolver.clear_browser_profile()
            self.app.log("Browser profile reset.")
            messagebox.showinfo("Reset", "Browser profile cleared.", parent=self)

    def _clear_cache(self) -> None:
        self.app.store.clear_listings()
        self.app.index._cache.clear()
        self.app.log("Index cache cleared.")
        messagebox.showinfo("Cache", "Index cache cleared.", parent=self)

    def _save(self) -> None:
        s = self.app.settings
        folder = self.dir_var.get().strip()
        if not folder:
            messagebox.showerror("Settings", "Choose a download folder.", parent=self)
            return
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Settings", f"Cannot create that folder:\n{exc}", parent=self)
            return

        s.download_dir = folder
        s.idm_path = self.idm_var.get().strip()
        s.queue_only = bool(self.queue_var.get())
        s.preserve_folders = bool(self.preserve_var.get())
        s.skip_existing = bool(self.skip_var.get())
        s.only_media = bool(self.media_var.get())
        exts = [x.strip().lower() for x in self.exts_var.get().replace(",", " ").split() if x.strip()]
        s.media_exts = [x if x.startswith(".") else "." + x for x in exts] or s.media_exts
        s.save()

        self.app.idm.path = s.idm_path or find_idm()
        self.app.index.ttl = s.index_ttl_minutes * 60
        self.app.log("Settings saved.")
        self.app._check_idm()
        self.destroy()


def run_selftest() -> int:
    """Check that the bundled browser driver and Chrome/Edge can start."""
    lines: list[str] = []
    ok = False
    try:
        import patchright
        lines.append(f"patchright: {getattr(patchright, '__version__', 'unknown')}")
        lines.append(f"package: {patchright.__file__}")
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = None
            for channel in ("chrome", "msedge"):
                try:
                    browser = pw.chromium.launch(channel=channel, headless=True)
                    lines.append(f"browser: {channel} started")
                    break
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"browser {channel} failed: {exc}")
            if browser is not None:
                page = browser.new_page()
                page.goto("data:text/html,<title>ok</title>selftest")
                title = page.title()
                lines.append(f"page title: {title}")
                ok = title == "ok"
                browser.close()
    except Exception:  # noqa: BLE001
        lines.append(traceback.format_exc())

    lines.append("RESULT: " + ("OK" if ok else "FAILED"))
    try:
        (DATA_DIR / "selftest.log").write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 1


def main() -> None:
    setup_logging()

    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())

    def thread_error(args) -> None:
        logging.error(
            "Unhandled error in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_error
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
