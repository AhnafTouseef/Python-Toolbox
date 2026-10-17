from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtCore import QProcess

from app.session_manager import ToolInstance


class ProcessManager(QObject):
    output_received = Signal(str, str)
    status_changed = Signal(str, str)
    process_ready = Signal(str)
    process_failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.running: Dict[str, QProcess] = {}

    def start_process(self, instance: ToolInstance, main_script: Path, interpreter: str) -> str:
        if instance.instance_id in self.running:
            self.running[instance.instance_id].kill()

        process = QProcess(self)
        process.setWorkingDirectory(str(instance.tool_dir))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        def on_ready_read() -> None:
            data = process.readAllStandardOutput()
            if data:
                text = data.data().decode("utf-8", errors="replace")
                instance.append_output(text)
                self.output_received.emit(instance.instance_id, text)

        def on_finished(code: int, status: QProcess.ExitStatus) -> None:
            self.running.pop(instance.instance_id, None)
            instance.process_id = None
            if status == QProcess.ExitStatus.CrashExit:
                instance.status = "Failed"
                self.status_changed.emit(instance.instance_id, "Failed")
            elif code == 0:
                instance.status = "Completed"
                self.status_changed.emit(instance.instance_id, "Completed")
            else:
                instance.status = f"Failed (exit {code})"
                self.status_changed.emit(instance.instance_id, "Failed")
            self.process_ready.emit(instance.instance_id)

        process.readyReadStandardOutput.connect(on_ready_read)
        process.finished.connect(on_finished)

        process.start(interpreter, [str(main_script)])
        if not process.waitForStarted(2000):
            instance.status = "Launch failed"
            self.process_failed.emit(instance.instance_id, f"Could not launch {main_script.name}.")
            return "failed"

        instance.status = "Running"
        instance.process_id = str(process.processId())
        self.running[instance.instance_id] = process
        self.status_changed.emit(instance.instance_id, "Running")
        return "started"

    def stop_process(self, instance: ToolInstance) -> bool:
        process = self.running.get(instance.instance_id)
        if not process:
            return False
        if process.state() == QProcess.ProcessState.Running:
            process.terminate()
            if not process.waitForFinished(2000):
                process.kill()
                process.waitForFinished(1000)
            instance.status = "Stopped"
            self.status_changed.emit(instance.instance_id, "Stopped")
            return True
        return False

    def write_stdin(self, instance: ToolInstance, text: str) -> None:
        process = self.running.get(instance.instance_id)
        if process and process.state() == QProcess.ProcessState.Running:
            payload = (text + "\n").encode("utf-8")
            process.write(payload)
