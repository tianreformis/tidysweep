# TidySweep

A small, open Windows cleaning utility written in Python (Tkinter) — a
clean-room CCleaner-style tool: original code and UI, no proprietary assets.

## Run it

Double-click `TidySweep.bat`, or:

```bat
python "C:\Users\SERVER SMA - 001\tidysweep\tidysweep.py"
```

If you want the console hidden, it already uses `pythonw` when launched via
the `.bat` file.

## What it cleans

| Category | Scope | Notes |
|---|---|---|
| User temp files | `%TEMP%` | safe |
| Windows temp files | `C:\Windows\Temp` | needs administrator (rest of app works without) |
| Chrome cache | Chrome profile caches (Cache, Code Cache, GPUCache, Media Cache) | does **not** touch cookies, history or passwords |
| Edge cache | same structure as Chrome | does **not** touch sign-in data |
| Firefox cache | `profile\cache2` | does **not** touch cookies/history |
| Thumbnail cache | `thumbcache_*.db` / `iconcache_*.db` | files may be locked while Explorer runs; they are skipped and reported |
| Recycle Bin | all drives, via `SHQueryRecycleBinW` / `SHEmptyRecycleBinW` | Windows confirmation is suppressed within the app's own confirm dialog |

## Safety design

- **Scan first, clean second.** Deletion only happens after an explicit
  confirmation dialog listing the selected categories.
- **Files only.** Folder structure is always preserved; the app deletes
  individual files.
- **No junction/symlink traversal.** Anything with a reparse point is never
  followed, so a stray junction can't redirect cleanup at other data.
- **Locked files are skipped**, never forced, and the skip count is reported.
- **No killing of browsers.** If a cache file is in use it is simply skipped.
- Categories selected are stored in `%APPDATA%\TidySweep\settings.json`.

## Structure

- `tidysweep.py` — the entire app (scanner, worker thread, Tk GUI)
- `TidySweep.bat` — double-click launcher
