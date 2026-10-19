"""
TidySweep - a small Windows cleaning utility (clean-room implementation).

Cleans user/system temp files, common browser caches, thumbnails, and the
Recycle Bin. Everything is scanned first, sizes are shown, and only files
are deleted (directory structure is preserved). Locked or in-use files are
skipped and reported. Nothing outside the selected categories is touched.

Run: python tidysweep.py
"""

import ctypes
import glob
import json
import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
SHERB_FLAGS = 0x00000001 | 0x00000002 | 0x00000004  # no confirm / no progress UI / no sound

APP_NAME = "TidySweep"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")


def is_user_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_reparse_point(path: str) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True  # refuse to descend when in doubt


def walk_files(root: str):
    """Yield file paths below root, without following junctions/symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        pruned = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if is_reparse_point(full):
                pruned.append(full)
        for p in pruned:
            try:
                dirnames.remove(p)
            except ValueError:
                pass
        for name in filenames:
            yield os.path.join(dirpath, name)


def local_appdata() -> str:
    return os.environ.get("LOCALAPPDATA",
                          os.path.join(os.path.expanduser("~"), "AppData", "Local"))


def _list_drives():
    import string
    drives = []
    for letter in string.ascii_uppercase:
        drive = letter + ":"
        if os.path.exists(drive + os.sep):
            drives.append(drive)
    return drives if drives else ["C:"]


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def _user_temp():
    yield from walk_files(tempfile.gettempdir())

def _system_temp():
    yield from walk_files(r"C:\Windows\Temp")

def _chromium(kind):
    base = os.path.join(local_appdata(), kind, "User Data")
    if not os.path.isdir(base):
        return
    for profile_entry in os.listdir(base):
        is_profile = profile_entry == "Default" or profile_entry.startswith("Profile")
        if not is_profile:
            continue
        profile_dir = os.path.join(base, profile_entry)
        if not os.path.isdir(profile_dir):
            continue
        for sub in ("Cache", "Code Cache", "GPUCache", "Media Cache"):
            nested = os.path.join(profile_dir, sub)
            targets = [nested]
            if sub == "Cache":
                targets.append(os.path.join(nested, "Cache_Data"))
            for t in targets:
                if os.path.isdir(t):
                    yield from walk_files(t)

def _chrome():
    yield from _chromium("Google\\Chrome")

def _edge():
    yield from _chromium("Microsoft\\Edge")

def _firefox():
    base = os.path.join(local_appdata(), "Mozilla", "Firefox", "Profiles")
    if not os.path.isdir(base):
        return
    for profile in os.listdir(base):
        cache2 = os.path.join(base, profile, "cache2")
        if os.path.isdir(cache2):
            yield from walk_files(cache2)

def _thumbnails():
    for pat in (
        os.path.join(local_appdata(), "Microsoft", "Windows", "Explorer", "thumbcache_*.db"),
        os.path.join(local_appdata(), "Microsoft", "Windows", "Explorer", "iconcache_*.db"),
    ):
        for hit in glob.glob(pat):
            yield hit

def _recycle_bin_placeholder():
    return iter(())  # recycle bin takes a dedicated code path

CATEGORIES = [
    {"name": "User temp files",
     "description": "Files in your %TEMP% folder",
     "files": _user_temp, "requires_admin": False},
    {"name": "Windows temp files",
     "description": r"C:\Windows\Temp (needs administrator)",
     "files": _system_temp, "requires_admin": True},
    {"name": "Chrome cache",
     "description": "Google Chrome browser cache",
     "files": _chrome, "requires_admin": False},
    {"name": "Edge cache",
     "description": "Microsoft Edge browser cache",
     "files": _edge, "requires_admin": False},
    {"name": "Firefox cache",
     "description": "Mozilla Firefox disk cache",
     "files": _firefox, "requires_admin": False},
    {"name": "Thumbnail cache",
     "description": "Windows thumbnail/icon databases (often locked)",
     "files": _thumbnails, "requires_admin": False},
    {"name": "Recycle Bin",
     "description": "Items in the Recycle Bin, all drives",
     "files": _recycle_bin_placeholder, "requires_admin": False},
]


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class Worker(threading.Thread):
    """Runs the scan/clean in the background; reports events via a queue."""

    def __init__(self, op, selections, out_queue):
        super().__init__(daemon=True)
        self.op = op                      # "scan" or "clean"
        self.selections = selections      # list of category dicts
        self.out = out_queue
        self.cancelled = False

    def emit(self, kind, *args):
        self.out.put((kind, args))

    def run(self):
        try:
            if self.op == "scan":
                self.scan()
            else:
                self.clean()
            self.emit("done")
        except Exception as exc:
            self.emit("error", f"{type(exc).__name__}: {exc}")

    # -- recycle bin via the Shell API -------------------------------------
    def query_recycle_bin(self):
        class _QueryRecycle(ctypes.Structure):
            _fields_ = [("i64Size", ctypes.c_ulonglong),
                        ("iNumItems", ctypes.c_ulong)]
        size = 0
        count = 0
        for drive in _list_drives():
            if self.cancelled:
                return 0, 0
            struct = _QueryRecycle()
            try:
                res = ctypes.windll.shell32.SHQueryRecycleBinW(
                    ctypes.c_wchar_p(drive + os.sep), ctypes.byref(struct))
            except Exception:
                res = 0x80070002
            if res == 0 and struct.iNumItems > 0:
                size += struct.i64Size
                count += struct.iNumItems
        return size, count

    def empty_recycle_bin(self):
        try:
            res = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_FLAGS)
        except Exception:
            res = 1
        return res == 0

    # -- main passes --------------------------------------------------------
    def scan(self):
        for cat in self.selections:
            if self.cancelled:
                return
            if cat["name"] == "Recycle Bin":
                size, count = self.query_recycle_bin()
                self.emit("end_category", cat["name"], size, count)
                continue
            if cat["requires_admin"] and not is_user_admin():
                self.emit("skip_admin", cat["name"])
                continue
            size = 0
            count = 0
            for path in cat["files"]():
                if self.cancelled:
                    self.emit("end_category", cat["name"], size, count)
                    return
                try:
                    size += os.path.getsize(path)
                    count += 1
                except OSError:
                    continue
            self.emit("end_category", cat["name"], size, count)

    def clean(self):
        for cat in self.selections:
            if self.cancelled:
                break
            if cat["name"] == "Recycle Bin":
                size, count = self.query_recycle_bin()
                if count > 0:
                    if self.empty_recycle_bin():
                        self.emit("end_category", cat["name"], size, count)
                    else:
                        self.emit("skipped", cat["name"], count)
                continue
            if cat["requires_admin"] and not is_user_admin():
                self.emit("skip_admin", cat["name"])
                continue
            deleted = 0
            freed = 0
            skipped = 0
            for path in cat["files"]():
                if self.cancelled:
                    break
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                    deleted += 1
                    freed += size
                except PermissionError:
                    skipped += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    skipped += 1
            self.emit("end_category", cat["name"], freed, deleted)
            if skipped:
                self.emit("skipped", cat["name"], skipped)


def human_size(n) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < step:
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= step
    return f"{n:,.1f} TB"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.queue = queue.Queue()
        self.worker = None
        self.checkbuttons = {}

        root.title(APP_NAME)
        root.geometry("720x560")
        root.minsize(640, 480)

        header = tk.Frame(root)
        header.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(header, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(side="left")
        admin = is_user_admin()
        tk.Label(header, text="Administrator" if admin else "Standard user",
                 fg="#0a7d32" if admin else "#8a6d1a").pack(side="right")

        body = tk.Frame(root)
        body.pack(fill="both", expand=True, padx=12)

        tk.Label(body, text="Nothing is deleted until you press Clean. "
                            "Locked files are always skipped.", anchor="w").pack(fill="x", pady=(0, 4))
        self.cat_box = tk.Frame(body)
        self.cat_box.pack(fill="x")

        for cat in CATEGORIES:
            var = tk.BooleanVar(value=False)
            tk.Checkbutton(self.cat_box,
                           text=f"{cat['name']}\u2003\u2014\u2003{cat['description']}",
                           variable=var, anchor="w").pack(fill="x")
            self.checkbuttons[cat["name"]] = var

        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=10)
        self.scan_btn = tk.Button(buttons, text="Scan", width=12, command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.clean_btn = tk.Button(buttons, text="Clean", width=12,
                                   command=self.start_clean, state="disabled")
        self.clean_btn.pack(side="left", padx=6)
        self.cancel_btn = tk.Button(buttons, text="Cancel", width=10,
                                    command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Select categories, then Scan.")
        tk.Label(root, textvariable=self.status_var, anchor="w",
                 relief="groove").pack(fill="x", padx=12)

        log_frame = tk.Frame(root)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        self.log = tk.Text(log_frame, height=12, state="disabled",
                           bg="#111", fg="#ddd", insertbackground="#ddd")
        scroll = tk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.load_settings()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll_queue)

    # ---- selected categories ----
    def selected_names(self):
        return [name for name, var in self.checkbuttons.items() if var.get()]

    def selected_categories(self):
        return [c for c in CATEGORIES if self.checkbuttons[c["name"]].get()]

    # ---- settings persistence ----
    def save_settings(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump({"categories": self.selected_names()}, fh)
        except OSError:
            pass

    def load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            for name in data.get("categories", []):
                if name in self.checkbuttons:
                    self.checkbuttons[name].set(True)
        except (OSError, ValueError):
            pass

    # ---- actions ----
    def start(self, op):
        selections = self.selected_categories()
        if not selections:
            messagebox.showinfo(APP_NAME, "Select at least one category first.")
            return
        self.save_settings()
        self.worker = Worker(op, selections, self.queue)
        self.set_busy(True)
        self.log_clear()
        verb = "Scanning" if op == "scan" else "Cleaning"
        self.log_write(f"{verb}: {', '.join(c['name'] for c in selections)} ...\n")
        self.status_var.set(f"{verb}\u2026")
        self.worker.start()

    def start_scan(self):
        self.start("scan")

    def start_clean(self):
        preview = "\n".join(self.selected_names())
        if not messagebox.askyesno(
                APP_NAME,
                f"Delete the files found in these categories?\n\n{preview}\n\nThis cannot be undone."):
            return
        self.start("clean")

    def cancel(self):
        if self.worker:
            self.worker.cancelled = True
            self.status_var.set("Cancelling\u2026")

    def set_busy(self, busy: bool):
        self.scan_btn.config(state="disabled" if busy else "normal")
        self.clean_btn.config(state="disabled")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    # ---- log ----
    def log_clear(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def log_write(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.config(state="disabled")

    # ---- queue pump ----
    def poll_queue(self):
        try:
            while True:
                kind, args = self.queue.get_nowait()
                if kind == "end_category":
                    name, size, count = args
                    self.log_write(f"  {name}: {count:,} files, {human_size(size)}\n")
                elif kind == "skip_admin":
                    self.log_write(f"  {args[0]}: needs administrator rights; skipped.\n")
                elif kind == "skipped":
                    self.log_write(f"  {args[0]}: {args[1]:,} item(s) skipped (in use/locked).\n")
                elif kind == "error":
                    messagebox.showerror(APP_NAME, "Worker failed:\n" + args[0])
                elif kind == "done":
                    if self.worker and self.worker.op == "scan":
                        self.status_var.set("Scan complete. Press Clean to delete the items above.")
                        self.clean_btn.config(state="normal")
                    else:
                        if self.status_var.get().endswith("\u2026"):
                            self.status_var.set("Done.")
                    self.set_busy(False)
                    self.worker = None
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def on_close(self):
        if self.worker:
            self.worker.cancelled = True
        self.save_settings()
        self.root.after(50, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
