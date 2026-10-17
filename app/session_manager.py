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
    output: List[str] = field(default_factory=list)
    input_text: str = ""
    run_count: int = 0
    process_id: Optional[str] = None

    def append_output(self, text: str) -> None:
        if not text:
            return
        lines = text.splitlines()
        if lines:
            for line in lines:
                self.output.append(line)


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
