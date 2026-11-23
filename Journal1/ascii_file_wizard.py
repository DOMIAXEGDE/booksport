#!/usr/bin/env python3
r"""A single-file, standard-library-only, full-screen ASCII file wizard.

Run: python ascii_file_wizard.py
Requires Python 3.10+ with Tkinter (included with standard Windows Python).
F11 toggles full screen; Escape leaves full screen; Ctrl+Enter advances.

Output is ASCII bytes with LF line endings, never platform-translated text:
    x12 ( 2026-11-22T22:30:00+00:00 ) {

        r0000   <exact row contents>
        r0001   <exact row contents>

    }
The indentation and the separator after each row ID are each one TAB byte.
LF separates numbered rows. All other ASCII bytes (0..127) are valid inside
a row, including CR and NUL. Blank rows and a final empty row are retained.
Empty editor content means one empty row. Row IDs start at zero and use a
minimum of four digits. File IDs are nonnegative decimal integers.

Literal mode never interprets backslashes. Escape mode supports \\, \t,
\r, \n, \0, \a, \b, \f, \v, and \xHH (00..7F). In escape mode a decoded
LF also separates rows. The visible preview escapes control characters;
the exported file contains the actual bytes. No third-party packages,
network access, or companion files are needed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import re
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "ASCII File Wizard"
LITERAL = "Literal ASCII"
ESCAPED = "ASCII escapes"
BG = "#101827"
PANEL = "#182438"
INPUT = "#0c1320"
FG = "#f1f5fc"
MUTED = "#a5b5cb"
ACCENT = "#68e0cd"
BORDER = "#34465e"
ERROR = "#ffb4aa"
PREVIEW_LIMIT = 150_000
CONTROL_NAMES = (
    "NUL", "SOH", "STX", "ETX", "EOT", "ENQ", "ACK", "BEL",
    "BS", "HT", "LF", "VT", "FF", "CR", "SO", "SI",
    "DLE", "DC1", "DC2", "DC3", "DC4", "NAK", "SYN", "ETB",
    "CAN", "EM", "SUB", "ESC", "FS", "GS", "RS", "US",
)


class InputError(ValueError):
    """User input cannot be represented by the requested file format."""


def validate_file_id(value: str) -> int:
    value = value.strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise InputError("Enter a nonnegative file ID using the digits 0–9.")
    normalized = value.lstrip("0") or "0"
    if len(normalized) > 200:
        raise InputError("The file ID is too long for a filename (maximum 200 digits).")
    return int(normalized)


def ascii_bytes(text: str) -> bytes:
    try:
        return text.encode("ascii")
    except UnicodeEncodeError as exc:
        pos = exc.start
        row = text.count("\n", 0, pos) + 1
        column = pos - text.rfind("\n", 0, pos)
        char = text[pos]
        raise InputError(
            f"Non-ASCII character {char!r} (U+{ord(char):04X}) at line {row}, "
            f"column {column}. Replace it with an ASCII character."
        ) from None


def decode_editor(text: str, mode: str) -> bytes:
    """Decode editable source, retaining LF as the only row separator."""
    source = ascii_bytes(text)
    if mode == LITERAL:
        return source
    if mode != ESCAPED:
        raise ValueError(f"Unknown editor mode: {mode}")
    result = bytearray()
    simple = {92: 92, 116: 9, 114: 13, 110: 10, 48: 0,
              97: 7, 98: 8, 102: 12, 118: 11}
    index = 0
    while index < len(source):
        char = source[index]
        if char != 92:
            result.append(char)
            index += 1
            continue
        start = index
        index += 1
        reason = "incomplete escape"
        if index < len(source):
            code = source[index]
            if code in simple:
                result.append(simple[code])
                index += 1
                continue
            if code == 120:
                pair = source[index + 1:index + 3]
                if len(pair) == 2 and re.fullmatch(rb"[0-9A-Fa-f]{2}", pair):
                    value = int(pair, 16)
                    if value <= 127:
                        result.append(value)
                        index += 3
                        continue
                    reason = "ASCII hex escapes must be between \\x00 and \\x7F"
                else:
                    reason = "\\x must be followed by exactly two hexadecimal digits"
            else:
                reason = f"unknown escape \\{chr(code)}; use \\\\ for a literal backslash"
        row = text.count("\n", 0, start) + 1
        column = start - text.rfind("\n", 0, start)
        raise InputError(f"Line {row}, column {column}: {reason}.")
    return bytes(result)


def escape_ascii(data: bytes, *, keep_tabs: bool = False) -> str:
    """Reversible display of ASCII bytes, with real LF between rows."""
    if any(value > 127 for value in data):
        raise InputError("Only ASCII bytes 0–127 are supported.")
    pieces = []
    for value in data:
        if value == 10:
            pieces.append("\n")
        elif value == 9:
            pieces.append("\t" if keep_tabs else r"\t")
        elif value == 13:
            pieces.append(r"\r")
        elif value == 92:
            pieces.append(r"\\")
        elif 32 <= value <= 126:
            pieces.append(chr(value))
        else:
            pieces.append(f"\\x{value:02X}")
    return "".join(pieces)


def format_document(file_id: int, payload: bytes, timestamp: str) -> bytes:
    """Build the exact export bytes. The timestamp includes a UTC offset."""
    if not isinstance(file_id, int) or file_id < 0:
        raise InputError("File ID must be a nonnegative integer.")
    if any(value > 127 for value in payload):
        raise InputError("Only ASCII bytes 0–127 are supported.")
    stamp = ascii_bytes(timestamp)
    if any(value < 32 or value == 127 for value in stamp):
        raise InputError("Timestamp cannot contain control characters.")
    rows = payload.split(b"\n")
    body = b"\n".join(
        f"\tr{index:04d}\t".encode("ascii") + row
        for index, row in enumerate(rows)
    )
    return b"x" + str(file_id).encode("ascii") + b" ( " + stamp + b" ) {\n\n" + body + b"\n\n}\n"


def next_available_id(directory: Path) -> int:
    file_id = 0
    while (directory / f"x{file_id}.txt").exists():
        file_id += 1
    return file_id


def write_new_file(destination: Path, data: bytes) -> None:
    """Exclusive binary creation prevents silent replacement and CRLF rewriting."""
    with destination.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class AsciiWizard(tk.Tk):
    def __init__(self, *, windowed: bool = False) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.geometry("1180x800")
        self.minsize(820, 590)
        self.full_screen = not windowed
        self.attributes("-fullscreen", self.full_screen)
        self.step = 0
        self.dirty = False
        self.saved_path: Path | None = None
        self.export_bytes: bytes | None = None
        self.active_mode = LITERAL
        self.pending_refresh: str | None = None
        self.gutter_ids: list[int] = []
        self.preview_time = ""
        initial_directory = Path(__file__).resolve().parent
        self.folder_var = tk.StringVar(value=str(initial_directory))
        self.id_var = tk.StringVar(value=str(next_available_id(initial_directory)))
        self.mode_var = tk.StringVar(value=LITERAL)
        self.stats_var = tk.StringVar(value="1 row  ·  0 content bytes  ·  ASCII ready")
        self.path_var = tk.StringVar()
        self.review_var = tk.StringVar()
        self.sidebar_var = tk.StringVar(value="x0.txt\n1 numbered row")
        self.control_var = tk.StringVar(value="09  HT — horizontal tab")
        self._style()
        self._build()
        self.folder_var.trace_add("write", self._details_changed)
        self.id_var.trace_add("write", self._details_changed)
        self.bind("<F11>", self.toggle_full_screen)
        self.bind("<Escape>", self.leave_full_screen)
        self.bind("<Control-Return>", self._advance_shortcut)
        self.bind("<Control-s>", self._save_shortcut)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._update_path()
        self.show_step(0)

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 11), background=BG,
                        foreground=FG, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, troughcolor=INPUT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 26, "bold"))
        style.configure("Heading.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Brand.TLabel", font=("Segoe UI", 12, "bold"), foreground=ACCENT)
        style.configure("Step.TLabel", background=PANEL, foreground=MUTED,
                        padding=(18, 15), font=("Segoe UI", 11))
        style.configure("Active.Step.TLabel", background="#233e4b", foreground=ACCENT,
                        font=("Segoe UI", 11, "bold"))
        style.configure("TButton", background="#26374e", foreground=FG,
                        padding=(16, 10), borderwidth=0)
        style.map("TButton", background=[("active", "#364d68"), ("disabled", PANEL)],
                  foreground=[("disabled", "#697c96")])
        style.configure("Accent.TButton", background=ACCENT, foreground=INPUT,
                        font=("Segoe UI", 11, "bold"))
        style.map("Accent.TButton", background=[("active", "#a0f2e4"), ("disabled", "#294a50")],
                  foreground=[("disabled", "#7b9699")])
        style.configure("TEntry", fieldbackground=INPUT, foreground=FG,
                        insertcolor=FG, padding=10, borderwidth=1)
        style.configure("TCombobox", fieldbackground=INPUT, foreground=FG,
                        padding=8, arrowcolor=FG, background=PANEL)
        style.map("TCombobox", fieldbackground=[("readonly", INPUT)],
                  foreground=[("readonly", FG)], selectbackground=[("readonly", INPUT)])
        style.configure("TSeparator", background=BORDER)
        self.option_add("*TCombobox*Listbox.background", INPUT)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", "#284b61")

    def _build(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        header = ttk.Frame(self, padding=(28, 16))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="[x]  ASCII FILE WIZARD", style="Brand.TLabel").pack(side="left")
        ttk.Button(header, text="Close", command=self.close).pack(side="right", padx=(10, 0))
        self.screen_button = ttk.Button(header, text="Leave full screen  ·  F11", command=self.toggle_full_screen)
        self.screen_button.pack(side="right")
        self._update_full_screen_label()
        body = ttk.Frame(self, padding=(24, 6, 24, 10))
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        side = ttk.Frame(body, style="Panel.TFrame", width=210)
        side.grid(row=0, column=0, sticky="ns", padx=(0, 28))
        side.grid_propagate(False)
        self.step_labels = []
        for index, title in enumerate(("File details", "Write contents", "Review & export")):
            label = ttk.Label(side, text=f"0{index + 1}   {title}", style="Step.TLabel")
            label.pack(fill="x", pady=(12 if index == 0 else 2, 2))
            self.step_labels.append(label)
        ttk.Label(side, textvariable=self.sidebar_var, background=PANEL,
                  foreground=ACCENT, font=("Consolas", 11), wraplength=170,
                  padding=(18, 24)).pack(fill="x")
        ttk.Label(side, text="100% local\nASCII bytes 0–127\nAutomatic row IDs",
                  background=PANEL, foreground=MUTED, padding=18,
                  wraplength=175, justify="left").pack(side="bottom", fill="x")
        self.stage = ttk.Frame(body)
        self.stage.grid(row=0, column=1, sticky="nsew")
        self.stage.grid_columnconfigure(0, weight=1)
        self.stage.grid_rowconfigure(0, weight=1)
        self.pages = [ttk.Frame(self.stage) for _ in range(3)]
        for page in self.pages:
            page.grid(row=0, column=0, sticky="nsew")
        self._build_details(self.pages[0])
        self._build_editor(self.pages[1])
        self._build_review(self.pages[2])
        footer = ttk.Frame(self, padding=(28, 12, 28, 22))
        footer.grid(row=2, column=0, sticky="ew")
        self.back_button = ttk.Button(footer, text="Back", command=self.go_back)
        self.back_button.pack(side="left")
        ttk.Label(footer, text="Esc leaves full screen  ·  Ctrl+Enter continues",
                  style="Muted.TLabel").pack(side="left", padx=20)
        self.next_button = ttk.Button(footer, text="Continue", style="Accent.TButton", command=self.advance)
        self.next_button.pack(side="right")

    def _title(self, parent: ttk.Frame, title: str, subtitle: str) -> None:
        ttk.Label(parent, text=title, style="Title.TLabel").pack(anchor="w", pady=(7, 8))
        label = ttk.Label(parent, text=subtitle, style="Muted.TLabel", wraplength=770)
        label.pack(fill="x", pady=(0, 22))
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(260, event.width)))

    def _build_details(self, page: ttk.Frame) -> None:
        self._title(page, "Start a new file", "Choose the integer in x<int-id>.txt and where to save it.")
        ttk.Label(page, text="Destination folder", style="Heading.TLabel").pack(anchor="w", pady=(4, 10))
        folder_row = ttk.Frame(page)
        folder_row.pack(fill="x")
        ttk.Entry(folder_row, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="Browse…", command=self.choose_folder).pack(side="left", padx=(10, 0))
        ttk.Label(page, text="File ID", style="Heading.TLabel").pack(anchor="w", pady=(25, 10))
        id_row = ttk.Frame(page)
        id_row.pack(fill="x")
        self.id_entry = ttk.Entry(id_row, textvariable=self.id_var, width=18)
        self.id_entry.pack(side="left")
        ttk.Button(id_row, text="Next available ID", command=self.choose_next_id).pack(side="left", padx=10)
        ttk.Label(page, textvariable=self.path_var, foreground=ACCENT, wraplength=760).pack(anchor="w", pady=(14, 18))
        ttk.Separator(page).pack(fill="x", pady=(4, 20))
        ttk.Label(page, text="Your output format", style="Heading.TLabel").pack(anchor="w")
        sample = "x12 ( 2026-11-22T22:30:00+00:00 ) {\n\n    r0000    First line\n    r0001    Second line\n\n}"
        tk.Label(page, text=sample, font=("Consolas", 12), bg=INPUT, fg=FG,
                 justify="left", anchor="w", padx=18, pady=14).pack(fill="x", pady=(12, 10))
        ttk.Label(page, text="The file uses real tabs for indentation and a local timestamp with UTC offset.",
                  style="Muted.TLabel", wraplength=720).pack(anchor="w")

    def _make_text(self, parent: tk.Misc, **kwargs: object) -> tk.Text:
        return tk.Text(parent, bg=INPUT, fg=FG, insertbackground=ACCENT,
                       selectbackground="#27556a", selectforeground=FG,
                       font=("Consolas", 12), wrap="none", undo=True,
                       relief="flat", borderwidth=0, padx=12, pady=12,
                       tabs=("4c",), **kwargs)

    def _build_editor(self, page: ttk.Frame) -> None:
        self._title(page, "Write the contents", "Each newline creates the next numbered row. Spaces, blank rows, and tabs are preserved.")
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=(0, 10))
        self.mode_combo = ttk.Combobox(toolbar, textvariable=self.mode_var,
                                       values=(LITERAL, ESCAPED), state="readonly", width=17)
        self.mode_combo.pack(side="left")
        self.mode_combo.bind("<<ComboboxSelected>>", self.change_mode)
        ttk.Button(toolbar, text="Import ASCII text…", command=self.import_text).pack(side="left", padx=10)
        ttk.Button(toolbar, text="Entry help", command=self.show_help).pack(side="right")
        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(0, 12))
        ttk.Label(controls, text="Insert character").pack(side="left", padx=(0, 10))
        names = []
        for code in range(128):
            if code < 32:
                name = CONTROL_NAMES[code]
                if code == 9:
                    name += " — horizontal tab"
                elif code == 10:
                    name += " — new numbered row"
            elif code == 32:
                name = "SPACE"
            elif code == 127:
                name = "DEL"
            else:
                name = chr(code)
            names.append(f"{code:02X}  {name}")
        ttk.Combobox(controls, textvariable=self.control_var, values=names,
                     width=28, state="readonly").pack(side="left")
        ttk.Button(controls, text="Insert", command=self.insert_character).pack(side="left", padx=10)
        editor_box = ttk.Frame(page)
        editor_box.pack(fill="both", expand=True)
        editor_box.grid_columnconfigure(1, weight=1)
        editor_box.grid_rowconfigure(0, weight=1)
        self.gutter = tk.Canvas(editor_box, width=86, bg=PANEL, highlightthickness=0)
        self.gutter.grid(row=0, column=0, sticky="ns")
        self.editor = self._make_text(editor_box, height=8)
        self.editor.grid(row=0, column=1, sticky="nsew")
        vertical = ttk.Scrollbar(editor_box, orient="vertical", command=self.editor.yview)
        vertical.grid(row=0, column=2, sticky="ns")
        self.editor.configure(yscrollcommand=lambda first, last: self._scroll_editor(vertical, first, last))
        horizontal = ttk.Scrollbar(editor_box, orient="horizontal", command=self.editor.xview)
        horizontal.grid(row=1, column=1, sticky="ew")
        self.editor.configure(xscrollcommand=horizontal.set)
        self.editor.bind("<<Modified>>", self._editor_changed)
        self.editor.bind("<Configure>", lambda event: self.draw_gutter())
        self.editor.bind("<Tab>", self.insert_tab)
        self.editor.bind("<Control-Tab>", self.focus_after_editor)
        self.editor.bind("<Control-a>", self.select_all)
        self.editor.bind("<Control-Return>", self._advance_shortcut)
        self.editor.bind("<Control-s>", self._save_shortcut)
        self.editor.bind("<<Paste>>", self.paste_ascii)
        self.stats_label = ttk.Label(page, textvariable=self.stats_var, style="Muted.TLabel", wraplength=760)
        self.stats_label.pack(fill="x", pady=(10, 5))
        ttk.Label(page, text="Tab inserts a tab. Ctrl+Tab leaves the editor. Use escape mode for invisible controls.",
                  style="Muted.TLabel", wraplength=760).pack(anchor="w")

    def _build_review(self, page: ttk.Frame) -> None:
        self._title(page, "Review & export", "Check the numbered rows, then save your ASCII .txt file.")
        ttk.Label(page, textvariable=self.review_var, foreground=ACCENT, wraplength=760).pack(fill="x", pady=(0, 12))
        box = ttk.Frame(page)
        box.pack(fill="both", expand=True)
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(0, weight=1)
        self.preview = self._make_text(box, height=8, state="disabled")
        self.preview.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(box, orient="vertical", command=self.preview.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(box, orient="horizontal", command=self.preview.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.preview.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.preview_note = ttk.Label(page, text="Visible preview: control bytes use \\t, \\r, or \\xHH; backslashes use \\\\. Export contains the actual bytes.",
                                      style="Muted.TLabel", wraplength=760)
        self.preview_note.pack(fill="x", pady=(10, 12))
        self.saved_actions = ttk.Frame(page)
        ttk.Button(self.saved_actions, text="Open destination folder", command=self.open_folder).pack(side="left")
        ttk.Button(self.saved_actions, text="New file", command=self.new_file).pack(side="left", padx=10)

    def _editor_text(self) -> str:
        return self.editor.get("1.0", "end-1c")

    def _payload(self) -> bytes:
        return decode_editor(self._editor_text(), self.active_mode)

    def _replace_editor(self, text: str) -> None:
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_reset()
        self.editor.edit_modified(False)
        self._invalidate_saved()
        self.refresh_stats()

    def _invalidate_saved(self) -> None:
        self.saved_path = None
        self.export_bytes = None
        self.dirty = True
        self.saved_actions.pack_forget()
        if self.step == 2:
            self.next_button.configure(text="Save .txt file", state="normal")

    def _details_changed(self, *_: object) -> None:
        self._invalidate_saved()
        self._update_path()

    def _update_path(self) -> None:
        try:
            file_id = validate_file_id(self.id_var.get())
            path = Path(self.folder_var.get().strip()).expanduser() / f"x{file_id}.txt"
            suffix = "  ·  Already exists; choose another ID." if path.exists() else ""
            self.path_var.set(f"{path}{suffix}")
            count = self._editor_text().count("\n") + 1
            self.sidebar_var.set(f"x{file_id}.txt\n{count:,} numbered {'row' if count == 1 else 'rows'}")
        except (InputError, OSError, ValueError):
            self.path_var.set("Enter a valid folder and a nonnegative integer ID.")

    def _destination(self) -> Path:
        file_id = validate_file_id(self.id_var.get())
        folder = self.folder_var.get().strip()
        if not folder:
            raise InputError("Choose a destination folder.")
        directory = Path(folder).expanduser().resolve()
        if not directory.is_dir():
            raise InputError("The destination folder does not exist. Choose an existing folder.")
        return directory / f"x{file_id}.txt"

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="Choose a destination folder",
                                         initialdir=self.folder_var.get(), mustexist=True)
        if folder:
            self.folder_var.set(folder)
            self.choose_next_id()

    def choose_next_id(self) -> None:
        try:
            folder = Path(self.folder_var.get().strip()).expanduser()
            if not folder.is_dir():
                raise InputError("Choose an existing destination folder first.")
            self.id_var.set(str(next_available_id(folder)))
        except (OSError, ValueError) as exc:
            self._error(str(exc))

    def show_step(self, index: int) -> None:
        self.step = index
        self.pages[index].tkraise()
        for position, label in enumerate(self.step_labels):
            label.configure(style="Active.Step.TLabel" if position == index else "Step.TLabel")
        self.back_button.configure(state="disabled" if index == 0 else "normal")
        label = ("Write contents", "Review file", "Save .txt file")[index]
        self.next_button.configure(text=label, state="normal")
        if index == 0:
            self.after_idle(self.id_entry.focus_set)
        elif index == 1:
            self.after_idle(self.editor.focus_set)
            self.after_idle(self.draw_gutter)
        elif self.saved_path:
            self.next_button.configure(text="Saved", state="disabled")

    def advance(self) -> None:
        try:
            if self.step == 0:
                path = self._destination()
                if path.exists():
                    raise InputError(f"{path.name} already exists. Select Next available ID or enter a different ID.")
                self.id_var.set(str(validate_file_id(self.id_var.get())))
                self.show_step(1)
            elif self.step == 1:
                if not self.saved_path:
                    self.prepare_preview()
                self.show_step(2)
            elif not self.saved_path:
                self.save_file()
        except (InputError, OSError, ValueError) as exc:
            self._error(str(exc))

    def go_back(self) -> None:
        if self.step > 0:
            self.show_step(self.step - 1)

    def _advance_shortcut(self, event: tk.Event) -> str:
        self.advance()
        return "break"

    def _save_shortcut(self, event: tk.Event) -> str:
        if self.step == 2 and not self.saved_path:
            self.advance()
        return "break"

    def _editor_changed(self, event: tk.Event) -> None:
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self._invalidate_saved()
            if self.pending_refresh:
                self.after_cancel(self.pending_refresh)
            self.pending_refresh = self.after(100, self.refresh_stats)

    def refresh_stats(self) -> None:
        self.pending_refresh = None
        try:
            payload = self._payload()
            count = payload.count(b"\n") + 1
            self.stats_var.set(f"{count:,} {'row' if count == 1 else 'rows'}  ·  {len(payload):,} content bytes  ·  ASCII ready")
            self.stats_label.configure(foreground=MUTED)
            try:
                file_id = validate_file_id(self.id_var.get())
                self.sidebar_var.set(f"x{file_id}.txt\n{count:,} numbered {'row' if count == 1 else 'rows'}")
            except InputError:
                pass
        except InputError as exc:
            self.stats_var.set(str(exc))
            self.stats_label.configure(foreground=ERROR)
        self._compute_gutter_ids()
        self.draw_gutter()

    def _compute_gutter_ids(self) -> None:
        self.gutter_ids = []
        next_id = 0
        for source_line in self._editor_text().split("\n"):
            self.gutter_ids.append(next_id)
            try:
                next_id += decode_editor(source_line, self.active_mode).count(b"\n") + 1
            except InputError:
                next_id += 1

    def _scroll_editor(self, scrollbar: ttk.Scrollbar, first: str, last: str) -> None:
        scrollbar.set(first, last)
        self.draw_gutter()

    def draw_gutter(self) -> None:
        self.gutter.delete("all")
        index = self.editor.index("@0,0")
        while True:
            geometry = self.editor.dlineinfo(index)
            if geometry is None:
                break
            line = int(index.split(".")[0])
            row_id = self.gutter_ids[line - 1] if line <= len(self.gutter_ids) else line - 1
            self.gutter.create_text(74, geometry[1], text=f"r{row_id:04d}", anchor="ne",
                                    fill=MUTED, font=("Consolas", 11))
            index = self.editor.index(f"{index}+1line")

    def _set_mode(self, mode: str) -> bool:
        if mode == self.active_mode:
            return True
        try:
            data = self._payload()
            if mode == LITERAL and any(byte < 32 and byte not in (9, 10) or byte == 127 for byte in data):
                raise InputError("This content includes invisible control bytes. Keep ASCII escapes mode to edit them safely.")
            self.active_mode = mode
            self.mode_var.set(mode)
            self._replace_editor(escape_ascii(data) if mode == ESCAPED else data.decode("ascii"))
            return True
        except InputError as exc:
            self.mode_var.set(self.active_mode)
            self._error(str(exc))
            return False

    def change_mode(self, event: tk.Event) -> None:
        self._set_mode(self.mode_var.get())

    def _insert_source(self, source: str) -> None:
        if self.editor.tag_ranges("sel"):
            self.editor.delete("sel.first", "sel.last")
        self.editor.insert("insert", source)
        self.editor.see("insert")
        self.editor.focus_set()

    def insert_character(self) -> None:
        value = int(self.control_var.get()[:2], 16)
        caret = self.editor.index("insert")
        if self.active_mode == LITERAL and (value < 32 and value not in (9, 10) or value == 127):
            # Map the current cursor/selection to the escaped representation.
            try:
                prefix = ascii_bytes(self.editor.get("1.0", caret))
                offset = len(escape_ascii(prefix))
                selection = self.editor.tag_ranges("sel")
                selection_offsets = None
                if selection:
                    selection_offsets = tuple(len(escape_ascii(ascii_bytes(self.editor.get("1.0", str(pos))))) for pos in selection)
            except InputError as exc:
                self._error(str(exc))
                return
            if not self._set_mode(ESCAPED):
                return
            self.editor.mark_set("insert", f"1.0+{offset}c")
            if selection_offsets:
                self.editor.tag_add("sel", f"1.0+{selection_offsets[0]}c", f"1.0+{selection_offsets[1]}c")
        source = escape_ascii(bytes([value])) if self.active_mode == ESCAPED else chr(value)
        self._insert_source(source)

    def insert_tab(self, event: tk.Event) -> str:
        self._insert_source(r"\t" if self.active_mode == ESCAPED else "\t")
        return "break"

    def focus_after_editor(self, event: tk.Event) -> str:
        self.editor.tk_focusNext().focus_set()
        return "break"

    def select_all(self, event: tk.Event) -> str:
        self.editor.tag_add("sel", "1.0", "end-1c")
        return "break"

    def paste_ascii(self, event: tk.Event) -> str:
        try:
            content = self.clipboard_get()
        except tk.TclError:
            return "break"
        # Clipboard text uses CRLF on Windows. Normalize only that transport
        # convention; raw imports and explicit escape entry never normalize CR.
        content = content.replace("\r\n", "\n")
        if self.active_mode == LITERAL and any(ord(c) < 32 and c not in "\t\n" or ord(c) == 127 for c in content):
            try:
                ascii_bytes(content)
            except InputError as exc:
                self._error(str(exc))
                return "break"
            self._error("Paste these controls in ASCII escapes mode, or import an ASCII file to preserve its exact bytes.")
            return "break"
        self._insert_source(content)
        return "break"

    def import_text(self) -> None:
        name = filedialog.askopenfilename(parent=self, title="Import raw ASCII contents",
                                         filetypes=[("Text files", "*.txt"), ("All files", "*")])
        if not name:
            return
        try:
            data = Path(name).read_bytes()
            if any(value > 127 for value in data):
                bad = next(index for index, value in enumerate(data) if value > 127)
                raise InputError(f"This file contains a non-ASCII byte at offset {bad}. Import requires bytes 0–127.")
            if self._editor_text() and not messagebox.askyesno("Replace editor contents?", "Replace the current contents with the imported file?", parent=self):
                return
            controls = any(byte < 32 and byte not in (9, 10) or byte == 127 for byte in data)
            self.active_mode = ESCAPED if controls else LITERAL
            self.mode_var.set(self.active_mode)
            self._replace_editor(escape_ascii(data) if controls else data.decode("ascii"))
        except (OSError, InputError) as exc:
            self._error(str(exc))

    def prepare_preview(self) -> None:
        path = self._destination()
        payload = self._payload()
        self.preview_time = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        data = format_document(validate_file_id(self.id_var.get()), payload, self.preview_time)
        self.export_bytes = data
        rows = payload.count(b"\n") + 1
        self.review_var.set(f"{path}\n{rows:,} numbered {'row' if rows == 1 else 'rows'}  ·  {len(data):,} file bytes  ·  {self.preview_time}")
        shown = escape_ascii(data[:PREVIEW_LIMIT])
        if len(data) > PREVIEW_LIMIT:
            shown += "\n\n[Preview shortened. The saved file will include all rows.]"
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", shown)
        self.preview.configure(state="disabled")

    def save_file(self) -> None:
        self.prepare_preview()
        path = self._destination()
        assert self.export_bytes is not None
        try:
            write_new_file(path, self.export_bytes)
        except FileExistsError:
            raise InputError(f"{path.name} already exists. Go back to File details and choose another ID.") from None
        self.saved_path = path
        self.dirty = False
        self.review_var.set(f"Saved successfully\n{path}\n{len(self.export_bytes):,} bytes  ·  {self.preview_time}")
        self.saved_actions.pack(fill="x", pady=(2, 0))
        self.next_button.configure(text="Saved", state="disabled")

    def open_folder(self) -> None:
        if not self.saved_path:
            return
        try:
            folder = str(self.saved_path.parent)
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as exc:
            self._error(f"Could not open the folder: {exc}")

    def new_file(self) -> None:
        if self.dirty and not messagebox.askyesno("Start a new file?", "Discard unsaved changes and start a new file?", parent=self):
            return
        self.active_mode = LITERAL
        self.mode_var.set(LITERAL)
        self._replace_editor("")
        self.choose_next_id()
        self.dirty = False
        self.show_step(0)

    def show_help(self) -> None:
        messagebox.showinfo("ASCII entry help", "Literal ASCII\nType or paste text normally. Backslashes remain literal. Tab inserts a tab, and Enter starts a numbered row.\n\nASCII escapes\nUse \\\\ for a backslash, \\t for tab, \\r for carriage return, \\n for a new numbered row, and \\xHH for any ASCII code from 00 through 7F. Also supported: \\0, \\a, \\b, \\f, \\v.\n\nThe character picker contains all 128 ASCII characters and switches to escape mode when needed. A source line containing \\n can produce multiple rows; the gutter shows its first row ID.\n\nEvery LF separates rows; all other ASCII bytes remain inside the row. Leading/trailing whitespace and final empty rows are retained. The preview shows escaped controls; export writes actual bytes.\n\nRaw import preserves every byte, including CRLF. Clipboard paste treats CRLF as a newline. Non-ASCII text must be corrected before export.", parent=self)

    def _update_full_screen_label(self) -> None:
        self.screen_button.configure(text="Leave full screen  ·  F11" if self.full_screen else "Full screen  ·  F11")

    def toggle_full_screen(self, event: tk.Event | None = None) -> str:
        self.full_screen = not self.full_screen
        self.attributes("-fullscreen", self.full_screen)
        self._update_full_screen_label()
        return "break"

    def leave_full_screen(self, event: tk.Event | None = None) -> str:
        if self.full_screen:
            self.full_screen = False
            self.attributes("-fullscreen", False)
            self._update_full_screen_label()
        return "break"

    def close(self) -> None:
        if self.dirty and not messagebox.askyesno("Close the wizard?", "You have unsaved changes. Close without saving?", parent=self):
            return
        self.destroy()

    def _error(self, text: str) -> None:
        messagebox.showerror(APP_NAME, text, parent=self)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create timestamped, numbered ASCII text files in a full-screen wizard.")
    parser.add_argument("--windowed", action="store_true", help="Start in a window instead of full screen.")
    args = parser.parse_args()
    app = AsciiWizard(windowed=args.windowed)
    app.mainloop()


if __name__ == "__main__":
    main()
