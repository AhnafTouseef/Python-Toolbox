from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ToolInstance:
    instance_id: str
    tool_name: str
    tool_dir: Path
    interpreter: str
    status: str = "Idle"
    output: str = ""
    input_text: str = ""
    run_count: int = 0
    process_id: Optional[str] = None

    def append_output(self, text: str) -> None:
        if not text:
            return
        self.output += text

    def rendered_output(self) -> str:
        """Render the terminal's current visible state, not the raw log history.

        A real terminal treats carriage return as a cursor reset on the current line,
        not as an instruction to keep appending older text. This implementation
        models the visible screen buffer so repeated output like `print(i, end="\r")`
        overwrites the previous value on the same line instead of creating stale log
        entries.
        """
        if not self.output:
            return ""

        if "\r" not in self.output and "\n" not in self.output and "\b" not in self.output:
            return self.output

        lines: List[str] = [""]
        row = 0
        col = 0

        def ensure_row(index: int) -> None:
            while len(lines) <= index:
                lines.append("")

        def write_char(ch: str) -> None:
            nonlocal row, col
            ensure_row(row)
            line = lines[row]
            if col < len(line):
                line = line[:col] + ch + line[col + 1:]
            else:
                line = line + (" " * (col - len(line))) + ch
            lines[row] = line
            col += 1

        for ch in self.output:
            if ch == "\r":
                col = 0
            elif ch == "\n":
                row += 1
                ensure_row(row)
                col = 0
            elif ch == "\b":
                if col > 0:
                    col -= 1
                    line = lines[row]
                    if col < len(line):
                        lines[row] = line[:col] + line[col + 1:]
                    else:
                        lines[row] = line[:-1]
            elif ch == "\x1b":
                continue
            else:
                write_char(ch)

        return "\n".join(lines)


class SessionManager:
    def __init__(self):
        self.instances: Dict[str, ToolInstance] = {}
        self.tabs: List[str] = []
        self.active_instance_id: Optional[str] = None

    def create_instance(self, tool_name: str, tool_dir: Path, interpreter: str) -> ToolInstance:
        instance_id = f"{tool_name.lower().replace(' ', '_')}_{len(self.instances) + 1}_{abs(hash(str(tool_dir)))}"
        instance = ToolInstance(
            instance_id=instance_id,
            tool_name=tool_name,
            tool_dir=tool_dir,
            interpreter=interpreter,
        )
        self.instances[instance_id] = instance
        self.tabs.append(instance_id)
        self.active_instance_id = instance_id
        return instance

    def get_instance(self, instance_id: str) -> Optional[ToolInstance]:
        return self.instances.get(instance_id)

    def get_active_instance(self) -> Optional[ToolInstance]:
        if self.active_instance_id is None:
            return None
        return self.instances.get(self.active_instance_id)

    def set_active_instance(self, instance_id: str) -> None:
        if instance_id in self.instances:
            self.active_instance_id = instance_id

    def close_instance(self, instance_id: str) -> None:
        if instance_id in self.instances:
            del self.instances[instance_id]
        if instance_id in self.tabs:
            self.tabs.remove(instance_id)
        if self.active_instance_id == instance_id:
            self.active_instance_id = self.tabs[-1] if self.tabs else None

    def reopen_existing_instance(self, tool_name: str, tool_dir: Path, interpreter: str) -> Optional[ToolInstance]:
        for instance in self.instances.values():
            if instance.tool_name == tool_name and instance.tool_dir == tool_dir:
                self.active_instance_id = instance.instance_id
                return instance
        return self.create_instance(tool_name, tool_dir, interpreter)
