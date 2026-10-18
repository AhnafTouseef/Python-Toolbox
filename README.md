# Python Toolbox

A dark, responsive Windows desktop launcher and session manager for your existing
command-line Python scripts. Instead of `cd`-ing into each tool's folder and running
`python main.py` in Command Prompt, you click a card, press **Run**, and watch output,
answer `input()` prompts, stop it, rerun it, or run several copies side by side.

It is a *launcher*, not a framework: your tools are never imported, modified, or asked to
follow any API. Each runs in its own folder with its own Python.

## Quick start (Windows 10/11)

```bat
cd PythonToolbox
python -m venv .venv-toolbox
.venv-toolbox\Scripts\activate
pip install -r requirements.txt
python app.py
```

Requires Python 3.10+ (3.13 was used for development). The toolbox's own dependency is
only `PySide6`; it is **separate** from your tools' dependencies, which it never installs
or manages. Use `pythonw app.py` if you do not want a console window.

Put tools in the `tools/` folder next to `app.py`:

```text
PythonToolbox/
├── app.py
├── config.json              (created on first run)
└── tools/
    ├── VideoProcessor/
    │   ├── main.py          <- required: this is what makes it a tool
    │   ├── tool.json        <- created/managed by the toolbox
    │   └── .venv/           <- optional: this tool's own environment
    └── DataAnalyzer/
        └── main.py
```

Tools are discovered **when the toolbox starts** (restart it after adding a folder).

## Framework choice: PySide6 (Qt 6)

| Option | Verdict |
|---|---|
| **PySide6** | **Chosen.** Native-feeling, GPU-free rendering that is fine on a dual-core i3; real layouts, resizing, dialogs, tab bars, animations (`QPropertyAnimation`), SVG rendering (`QtSvg`) for tinted Lucide icons, and a rich text widget that handles incremental terminal output. Official Qt for Python, LGPL. |
| PyQt6 | Same Qt, but GPL/commercial licensing is less convenient for a personal tool. |
| Tkinter | No dependencies, but custom dark styling, animated overlays, SVG tinting and a smooth responsive card grid would all be hand-built and fragile. |
| Electron / web UIs | Heavy for modest hardware and awkward for managing local Python subprocesses. |

Trade-offs: PySide6 adds a ~150 MB dependency, and Qt widgets need some care to stay
smooth with huge lists (hence the lazy library below). Process I/O deliberately does *not*
use `QProcess`: plain `subprocess.Popen` plus small reader threads gives exact control over
Windows console handling, Ctrl+C delivery and process-tree termination that a correct
**Stop** depends on.

## How it behaves

**Library.** A responsive grid (more columns when wide, tighter spacing when narrow) with
search. Cards are created lazily for the visible rows plus a small buffer; scrolling only
schedules work, never rebuilds anything, and the scroll range is fixed by arithmetic so the
scrollbar does not jump. Resizing re-places existing cards and keeps the row you were looking
at. Each card has a hover glow, a status pill (Running / Completed / Failed / Stopped / Launch
error) and a settings button (also **F2** or the context menu when a card is focused).

**Tool metadata.** `tool.json` holds exactly four fields: `name`, `description`, `icon`
(a bundled Lucide icon), `color` (hex). Missing or invalid fields are repaired one at a time;
valid customisations and unknown extra fields are preserved; a malformed file is backed up
before defaults are regenerated; write failures are reported and never claimed as saved. New
tools get a random *vivid* colour; in the settings dialog you can choose *any* colour,
including greys and white, with the wheel, brightness slider or hex field.

**Interpreter.** `tool/.venv/Scripts/python.exe` if it exists. If a `.venv` folder exists
but that file does not, you get a clear launch error — there is **no silent fallback**.
Otherwise the global interpreter is used: the Python running the toolbox, or
`"global_python"` in `config.json` (full path to a `python.exe`).

**Workspace.** Click a card to open its workspace: Back, instance tabs, `+`, Run/Stop, the
terminal, a stdin field and Clear.

* A tab is one *instance* (own process, output, input text, status). Several instances of
  one tool, and several tools, can run at once. Tab bar is shared across tools.
* Clicking a card returns to that tool's most recently active instance, or opens a first one
  (it is **not** started automatically — press Run). `+` adds another instance of the
  current tool.
* **Back** and switching tabs never stop anything. **Closing a tab** destroys the instance;
  if it is running you are asked first. Closing the last tab returns to the library.
* **Run** starts a fresh process and appends below the previous output with a dated
  separator. **Stop** sends Ctrl+C and, if the script has not exited after ~3 s, terminates it,
  then (~2 s later) kills the whole process tree. While stopping, the button becomes **Force
  stop** to skip the wait. Intentional stops show *Stopped*, not *Failed*.
* **Clear** only clears the display.
* Closing the application with running processes asks, then stops them.

**Active-process drawer.** The *Active* button slides a panel in from the right over the
library (about one third of the window, 300–460 px) listing each running instance. Click an
item to jump to that exact tab; close with ✕, Esc or by clicking outside. It never affects
processes or the grid underneath.

## Terminal behaviour and limitations

Scripts run through pipes, not a real console:

* `print()`, `input()`, tracebacks and exit codes work. The toolbox passes `-u` and sets
  `PYTHONUNBUFFERED=1` / `PYTHONIOENCODING=utf-8` so output and prompts appear immediately.
  Nothing else about the environment is changed.
* **stderr is merged into stdout** (like a console) so everything appears in the exact order
  it was written. Consequence: stderr is not shown in a different colour. (Separate pipes
  can show a traceback *before* the lines printed just ahead of it; set
  `ProcessRunner.merge_stderr = False` in `app/process_manager.py` if you prefer colour.)
* `\r` progress bars (tqdm etc.) update in place; ANSI colour/cursor sequences are removed.
* Not supported: full-screen/cursor-addressed programs (curses, vim-style UIs), anything that
  requires `sys.stdin.isatty()` to be true, and password prompts that read the console
  directly (`getpass` may fail or echo). GUI tools are out of scope.
* The display keeps the most recent 100 000 lines per instance.
* Output rate is flow-controlled: a script printing faster than the UI can draw is briefly
  slowed (it blocks on the pipe) instead of freezing the window or exhausting memory.

## Configuration

`config.json` (next to `app.py`): `{ "global_python": null }`. `null` = the interpreter running
the toolbox.

## Tests

```bat
pip install -r requirements.txt
set QT_QPA_PLATFORM=offscreen
python -m unittest discover -s tests -t .
```

118 tests cover discovery, metadata repair, colours, lazy rendering under scrolling and
resizing, settings/colour/icon dialogs, real child processes (stdin, working directory,
Unicode, large output, interrupt → terminate → kill, process trees, stubborn scripts,
ordering of stdout/stderr), tabs, closing rules, the drawer, and resizing while output
streams. Measured with 3 000 synthetic tools: ~32 cards exist at a time, the worst
scroll-handler call is under 1 ms, a warm start validates all metadata in ~0.1 s.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "…has a .venv folder, but its interpreter was not found" | The venv is missing `Scripts\python.exe` (e.g. created on another OS or broken). Recreate it (`python -m venv .venv`) or delete the `.venv` folder to use the global Python. |
| "The global Python interpreter was not found" | Set `"global_python"` in `config.json` to a real `python.exe`. |
| Tool needs packages and fails with `ModuleNotFoundError` | The toolbox never installs dependencies. Install them into the tool's `.venv` (or the global Python) yourself. |
| A new tool folder does not appear | It needs a `main.py` directly inside it (not in a sub-folder); restart the toolbox. |
| Banner "N notices while loading tool metadata" | A `tool.json` was invalid and was repaired (a malformed file is first copied to `tool.json.malformed-<timestamp>.bak`). **Details** lists each one. |
| "Could not save tool.json … not saved" | The folder is read-only or locked; nothing was changed. |
| Stop takes a few seconds | The script ignores Ctrl+C or is blocked; it is terminated after the grace period (use **Force stop**). |
| Output looks garbled for a non-Python subprocess | Child programs that write in the legacy console code page are decoded as UTF-8; characters may show as �. |
| Window closes but a process remains | Only possible if the toolbox is killed (Task Manager); a normal quit stops all child processes. |

Lucide icons are bundled locally (`assets/icons`, ISC/MIT, see `LICENSES/`); no network is needed.

## Project layout

```text
app.py                      entry point
app/
  main_window.py            pages (library / workspace) + drawer layer, navigation, quit/close rules
  library_view.py           responsive grid, lazy incremental rendering, search
  tool_card.py              card widget, hover glow, status pill
  tool_settings.py          settings dialog + icon picker
  color_picker.py           wheel / brightness / hex colour dialog
  tool_registry.py          discovery (never imports tools) + metadata updates
  tool_metadata.py          tool.json generate / validate / repair / backup
  icon_manager.py           bundled Lucide SVGs, tinting, caching
  process_manager.py        interpreter choice, subprocess I/O threads, staged stop
  session_manager.py        instances: status, run/stop, most-recent / running queries
  workspace_view.py         tabs, Run/Stop, per-instance panes
  terminal_model.py         line model (\r, \b, ANSI stripping) - no Qt
  terminal_view.py          incremental read-only terminal widget
  active_process_drawer.py  right-side overlay
  gc_guard.py               keeps Python's GC on the GUI thread (see source)
  theme.py                  dark palette and stylesheet
```

## Status

Developed and tested headless (Qt `offscreen`) on Linux, including every process-handling path
that exists on both platforms. The Windows-specific code — sending Ctrl+C through a console
helper and `taskkill /T /F` — follows documented Win32 behaviour but **has not been run on
Windows**; please exercise Stop on a real Windows machine (a script in `input()`, a
`time.sleep()` loop, and one that spawns a child process).
