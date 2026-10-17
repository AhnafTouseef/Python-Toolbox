# Python Toolbox for Windows

This project is a Windows desktop launcher for existing command-line Python tools. It keeps each tool in its own working directory, launches it with its own interpreter when available, and presents a clean library and execution workspace for monitoring output, rerunning scripts, and managing independent sessions.

## Framework choice

PySide6 was selected as the desktop framework for this project.

Why it fits:
- Native Windows desktop behavior with a responsive Qt UI
- Strong support for custom dark styling, dialogs, tabs, and overlays
- Excellent subprocess management through `QProcess`
- Reliable layout resizing and stable widget reuse for a tool library
- Good separation between UI components and process/session logic

Trade-offs:
- Qt introduces a larger runtime surface than a tiny Tkinter app
- More code is required to structure a complex launcher cleanly
- The project is intentionally opinionated around Windows desktop behavior rather than browser-like interfaces

The design prioritizes stable execution, process isolation, and responsive UI behavior over framework minimalism.

## Application goals

- Discover tools in the `tools/` directory by immediate subfolder
- Run each tool in its own working directory
- Use a tool-local `.venv/Scripts/python.exe` when present
- Reserve the global Python interpreter as the fallback
- Maintain independent running instances and session state
- Keep the library responsive and stable during scrolling and resizing
- Implement terminal output, stop controls, and instance tabs without blocking the UI thread

## Project structure

```text
PythonToolbox/
├── app.py
├── requirements.txt
├── README.md
├── LICENSES/
│   └── lucide.txt
├── app/
│   ├── __init__.py
│   ├── main_window.py
│   ├── library_view.py
│   ├── tool_settings.py
│   ├── process_manager.py
│   ├── session_manager.py
│   ├── terminal_view.py
│   ├── active_process_drawer.py
│   ├── icon_manager.py
│   ├── tool_metadata.py
│   └── theme.py
├── assets/
│   └── icons/
└── tools/
    ├── VideoProcessor/
    │   ├── main.py
    │   └── tool.json
    └── DataAnalyzer/
        ├── main.py
        └── tool.json
```

## Icon library

This project uses a bundled Lucide-inspired SVG icon set with local rendering. The icon data is bundled in the codebase and does not require internet access to display. The Lucide license information is stored in `LICENSES/lucide.txt`.

## Notes

- Existing tools are not rewritten or imported during discovery.
- Tools are launched with their working directory set to their own folder.
- Session state is kept in memory for the current application run.

## Running the project

From the project root:

```bash
python -m pip install -r requirements.txt
python app.py
```

## Troubleshooting

- If a tool has a `.venv` folder but not the expected `Scripts/python.exe`, launch will fail with a clear configuration error.
- If metadata is malformed, the app repairs it and preserves valid values when possible.
- If a tool has no `tool.json`, one is generated automatically with a default icon and accent color.

## License

See `LICENSES/lucide.txt` for bundled icon library licensing details.
