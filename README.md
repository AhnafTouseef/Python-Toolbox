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

Tools are discovered at start-up and whenever you press **Refresh** (the circular-arrow button in the library, or
**F5**): new, removed and edited tools show up without restarting. Open instances are never touched by a refresh
(an instance of a tool whose folder was deleted keeps its output; running it again reports the missing folder).

**More tool folders.** You do not have to move your tools into `tools/`. The folder button next to Refresh opens
**Tool folders**: add any number of folders, each holding tool folders (a folder with a `main.py`) exactly like
`tools/` does. `tools/` is **Path 1** and always stays; added folders are Path 2, 3, ... in the order listed and are
stored in `config.json` (`"tool_paths": ["D:/MyScripts", ...]`). A folder that is itself a tool is refused (add its
parent), a folder that disappears is reported and skipped, and a tool reachable through two overlapping folders
is listed once. If two tools have the same name, both are kept and their cards (and queue pickers, and tab
labels) carry a small tag naming the path they come from, e.g. `Path 2 · MyScripts`; the tag disappears when the
clash does. Tools from added folders run in their own folder with their own `.venv`, like any other tool.

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
  if it is running, the tab's ✕ asks first, while **double-clicking the tab** stops the process and
  closes it without asking (the quick way). Closing the last
  tab returns to the library.
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

## Sequential execution queue

Every instance (tab) has a **queue**: open it with the **Queue** button in the cockpit's title bar. It is a
right-side drawer that overlays the cockpit exactly like the active-process drawer (the terminal underneath is
never resized). Use it to prepare work while a long job is still running; the cells run one after another once
the main process has **exited**.

* **Notebook-style cells.** The drawer works like a notebook. In the blank space (and after the last cell) there
  are two buttons, **+ Code** and **+ Tool**; hovering over a cell opens the same two buttons right below it (and
  above the first cell), so a cell can be inserted anywhere between two others.
  *Code cell* - its code is edited right in the card. The language selector defaults to **Python**: the code is run
  by the tool's interpreter (its `.venv`, otherwise the global Python) in the tool's folder, exactly like a script
  (`__name__ == "__main__"`, the tool folder is importable, tracebacks show the source line). **Shell** runs the
  text as a command / batch script instead (Windows: `cmd.exe`; elsewhere `/bin/sh`). An empty cell completes at
  once without starting a process. *Tool cell* - any tool in `tools/`, launched through the same code as the Run
  button (a `.venv` without an interpreter is an error, never a silent fallback); the pencil button changes the tool.
  Adding or editing a cell never runs anything.
* **When it starts.** Only on the real exit event of the main process, never from output silence or timing.
  *Must Execute off*: the queue runs only if the main process exited with code 0 and was not stopped by you;
  otherwise the cells are kept untouched (state "not run") and run after the next successful run. It then stops at
  the first failed cell and marks the later ones *Skipped*. *Must Execute on*: it runs after any exit (including a
  main process that could not even be launched), and a failed cell never blocks the next one.
* **Strictly sequential.** One cell at a time, in the displayed order; the next one starts only after the previous
  process has exited and its output was delivered. Each cell has its own process, pipes, output and result; its
  input never reaches the main process or another cell.
* **Output and input below the cell.** Each cell shows what it printed in its own output area directly under
  it (the **Output** button hides it). While a cell runs, an input line sits right below that output, like
  `input()` in Jupyter: when the cell waits for input the line is highlighted and focused, type and press Enter.
* **Editable again after every run.** When a queue run ends, every cell goes back to *Pending* - editable,
  reorderable, removable - so the whole queue can be reused for the next run of the tool. What happened stays
  visible until the cell runs again ("Pending - last run: Completed in 0:03", with its output). While a cell is
  running its configuration is frozen; pending cells stay editable while another one runs. **Stop queue** stops
  the running cell (Ctrl+C, then terminate, then kill; a second press forces it) and cancels the rest.
* **Choosing a tool.** **+ Tool** opens a searchable list (name, description and folder name; type to filter,
  Up/Down to move while typing, Enter or double-click to confirm) instead of a dropdown, so it stays usable with
  hundreds of tools. Tools with the same name show their path.
* **Presets.** **Save as preset** stores the cells of the queue (code, language, names, tools, predefined
  answers - never Must Execute, results or output) under a name; **Presets...** lists them with a cell-by-cell
  preview and **Add to queue** appends the cells to the end of the current queue as if you had added them by hand
  (so they are fully editable, and importing twice is allowed). Presets are global: any tool's queue can use any
  preset. Each preset is one readable JSON file in the `presets/` folder next to `config.json`, so they can be
  copied or shared. Saving over an existing name asks first; presets can be renamed and deleted. A tool cell is
  saved by its folder name plus the folder it was found in, so presets keep working on another PC where an added
  folder lives elsewhere; if a tool cannot be found when importing you can **add the cell anyway** or **skip** it.
* **Predefined answers (paperclip).** `1920, 1080, yes` (whitespace around answers is trimmed; commas cannot be part
  of an answer; an empty entry between commas is an empty line). Answers are typed one per input request, only
  while the process is actually waiting, never all at once. Unused answers are reported; when a cell asks for more
  than were given, the cell's input line works as usual.
* **Session ownership.** The queue belongs to its instance and lives as long as the instance does: going back to
  the library does not touch it (the drawer closes, the queue keeps going), and finishing the main process does
  not discard it. Closing the instance (or quitting) stops a running cell and drops the rest - the queue is best
  effort **inside the running Toolbox**: a crash, a reboot or a power loss ends it, and there is no recovery.
  While the queue is executing the instance counts as running (library badge, active drawer, quit warning) and its
  Run button is disabled.

### Input detection and the 5-minute timeout (Must Execute)

A pipe does not tell the parent that the child is blocked on stdin, and silence or a trailing `?` prove nothing.
The reliable mechanism available is to ask the Python process itself: for queued cells the Toolbox puts
`app/stdin_probe/sitecustomize.py` on `PYTHONPATH` (together with a private temp folder). Python loads it at
startup; it rebuilds `sys.stdin` over a raw reader that drops a marker file while a **main-thread blocking read**
is in progress. The queue polls that folder, so:

| Cell | "Waiting for input" | Answers sent | 5-minute timeout (Must Execute on) |
|---|---|---|---|
| Tool cell, Python code cell, or `python ...` started by a shell cell | exact | when a read really blocks (one answer per request) | yes: blocked 300 s with no answer left -> stopped, **Failed - input timeout (5 minutes)**, the process is waited for, then the next cell starts |
| Anything else (`.exe`, `.bat` commands, `python -I`, ...) | not knowable, never claimed | when the output stops on an unfinished prompt line (e.g. `Continue? `) for 0.6 s | **never** (it could kill long work) |

The timeout measures *time blocked on input*, never running time or output silence: a three-hour render is never
touched. It restarts on every new request, and it does not exist when Must Execute is off. Known limits: input
read through `os.read(0)`, `msvcrt` or a helper thread is not seen (such a script is treated like a non-Python
program: no timeout); a Python program spawned by a cell that reads from its *own* pipe can look like a wait;
`PYTHONPATH` is prepended for queued cells, so a tool that inspects it would see one extra entry.
Cell output is one merged stream (like the main terminal) so a traceback stays below the lines printed before it;
`SessionQueue.merge_output = False` captures stderr separately (red), at the price of unordered interleaving.

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

`config.json` (next to `app.py`): `{ "global_python": null, "tool_paths": [] }`. `null` = the interpreter running
the toolbox; `tool_paths` = extra folders to look for tools in (managed by the folder button in the library).

## Tests

```bat
pip install -r requirements.txt
set QT_QPA_PLATFORM=offscreen
python -m unittest discover -s tests -t .
```

272 tests cover discovery, metadata repair, colours, lazy rendering under scrolling and
resizing, settings/colour/icon dialogs, real child processes (stdin, working directory,
Unicode, large output, interrupt → terminate → kill, process trees, stubborn scripts,
ordering of stdout/stderr), tabs, closing rules, the drawer, and resizing while output
streams, and the queue (strict ordering and no concurrency, Must Execute on/off, one-answer-per-prompt delivery,
the 5-minute timeout with a shortened clock, long-running cells never timed out, editing rules, teardown, the
stdin shim's transparency, drawer overlay and session ownership). Measured with 3 000 synthetic tools: ~32 cards exist at a time, the worst
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
  slide_drawer.py           shared overlay behaviour (layer, sliding panel, scrim, Esc)
  active_process_drawer.py  right-side overlay listing running instances
  queue_model.py            queue cells, statuses, answer parsing - no Qt
  session_queue.py          the sequential engine: Must Execute, answers, input timeout
  stdin_watch.py            Toolbox side of input detection (marker folder)
  stdin_probe/              sitecustomize.py injected into queued Python processes - nothing else may live here
  queue_drawer.py           queue drawer: notebook-style cells, insert bars, inline code/output/input, switch
  queue_dialogs.py          searchable tool picker, save-preset and presets dialogs
  presets.py                global queue presets (JSON files in presets/) - no Qt
  tool_folders.py           dialog to add / remove extra tool folders
  gc_guard.py               keeps Python's GC on the GUI thread (see source)
  error_reporting.py        unexpected-error dialog, always shown from the GUI thread
  theme.py                  dark palette and stylesheet
```

## Status

Developed and tested headless (Qt `offscreen`) on Linux, including every process-handling path
that exists on both platforms. The Windows-only code (Ctrl+C through a console helper,
`taskkill /T /F`) follows documented Win32 behaviour; a first real Windows run found one bug
in it (Windows exit codes such as `0xC000013A` exceed a signed 32-bit int), which is now fixed
and covered by tests that simulate such codes. Please keep exercising Stop on Windows with a
script in `input()`, a `time.sleep()` loop, and one that spawns a child process.

The queue's Windows-specific parts are also untested on a real Windows machine: Shell code cells run through a
generated `.cmd` file (`cmd.exe /d /s /c ""file""`, covered only by a test of the command line and file content),
and a timed-out cell is stopped through the same Ctrl+C/taskkill path as Stop. Python code cells use the same path as tool cells (the interpreter is started directly with a generated `.py`). Please try a Shell cell with a
quoted path, a Python code cell with `input()`, a Python tool cell with `input()` and predefined answers, and a short timeout trial.
