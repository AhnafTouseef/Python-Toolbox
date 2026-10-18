"""Line model for the terminal pane. Pure Python, no Qt, so it can be tested directly.

Pipes are not a console, so the terminal only needs to understand what ordinary
command-line scripts emit:

* ``\\n`` (and ``\\r\\n``) end a line.
* ``\\r`` returns to the start of the line and later text overwrites it
  (progress bars such as tqdm), ``\\b`` steps back, ``\\t`` pads to a tab stop.
* ANSI escape sequences are consumed and never shown.  ``ESC[K`` / ``ESC[0K`` /
  ``ESC[1K`` / ``ESC[2K`` (erase in line) are honoured because progress bars use
  them after ``\\r``; every other sequence (colours, cursor movement, screen
  clearing, window titles) is dropped.
* Other control characters are dropped.

The model keeps the *active* (last, possibly unfinished) line separately from the
lines that are finished.  The view asks for ``take()`` after feeding data and applies
the result to its document; the invariant is that the document's last block always
shows the active line.
"""

from __future__ import annotations

import re

STDOUT, STDERR, INPUT, SYSTEM = 0, 1, 2, 3
KIND_IDS = {"stdout": STDOUT, "stderr": STDERR, "input": INPUT, "system": SYSTEM}

MAX_LINE = 20000                 # a single unterminated line is wrapped after this many characters
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_CSI = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_MAX_CARRY = 1024                # longest incomplete escape sequence we wait for


Runs = list  # list[tuple[int, str]]: (kind id, text)


def _runs(text: str, kinds: bytearray) -> Runs:
    if not text:
        return []
    first = kinds[0]
    if kinds.count(first) == len(kinds):
        return [(first, text)]
    runs, start = [], 0
    for i in range(1, len(text) + 1):
        if i == len(text) or kinds[i] != kinds[start]:
            runs.append((kinds[start], text[start:i]))
            start = i
    return runs


class TerminalModel:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.text = ""
        self.kinds = bytearray()
        self.col = 0
        self._carry = ""
        self._finished: list[Runs] = []
        self._dirty = False

    # -- queries ---------------------------------------------------------------
    @property
    def line_is_empty(self) -> bool:
        return not self.text

    @property
    def active_text(self) -> str:
        return self.text

    # -- feeding -----------------------------------------------------------------
    def feed(self, kind: str, data: str) -> None:
        k = KIND_IDS.get(kind, STDOUT)
        if self._carry:
            data = self._carry + data
            self._carry = ""
        i, n = 0, len(data)
        while i < n:
            m = _CTRL.search(data, i)
            if m is None:
                self._put(data[i:], k)
                break
            j = m.start()
            if j > i:
                self._put(data[i:j], k)
            c = data[j]
            i = j + 1
            if c == "\n":
                self._newline()
            elif c == "\r":
                self.col = 0
                self._dirty = True
            elif c == "\b":
                self.col = max(0, self.col - 1)
            elif c == "\t":
                self._put(" " * (8 - self.col % 8), k)
            elif c == "\x1b":
                end = self._escape(data, j)
                if end < 0:                                   # sequence continues in the next chunk
                    rest = data[j:]
                    if len(rest) <= _MAX_CARRY:
                        self._carry = rest
                    break
                i = end
            # any other control character is ignored
        self._dirty = True

    def _escape(self, data: str, start: int) -> int:
        """Consume the escape sequence at data[start]; return the index after it, or -1 if incomplete."""
        n = len(data)
        if start + 1 >= n:
            return -1
        c = data[start + 1]
        if c == "[":
            m = _CSI.match(data, start)
            if m is None:
                # Not (yet) a complete CSI sequence. If the rest is only parameter bytes, wait for more.
                return -1 if re.fullmatch(r"\x1b\[[0-?]*[ -/]*", data[start:]) else start + 1
            if m.group(2) == "K":
                self._erase_in_line(m.group(1) or "0")
            return m.end()
        if c == "]":
            m = _OSC.match(data, start)
            if m is None:
                return -1 if "\x07" not in data[start:] and "\x1b\\" not in data[start:] else start + 1
            return m.end()
        if c in "()*+":                                       # charset selection: ESC ( B
            return start + 3 if start + 2 < n else -1
        return start + 2                                      # ESC <char>

    def _erase_in_line(self, mode: str) -> None:
        if mode in ("", "0"):
            self.text = self.text[:self.col]
            del self.kinds[self.col:]
        elif mode == "1":
            end = min(self.col, len(self.text))
            self.text = " " * end + self.text[end:]
        elif mode == "2":
            self.text = ""
            self.kinds = bytearray()

    def _put(self, s: str, k: int) -> None:
        while s:
            room = MAX_LINE - self.col
            if room <= 0:
                self._newline()
                continue
            chunk, s = s[:room], s[room:]
            self._write(chunk, k)
            if s:
                self._newline()

    def _write(self, s: str, k: int) -> None:
        col, n = self.col, len(self.text)
        if col == n:
            self.text += s
            self.kinds.extend(bytes((k,)) * len(s))
        else:
            if col > n:                                       # cursor beyond the text (after ESC[2K, \t ...)
                self.text += " " * (col - n)
                self.kinds.extend(bytes((k,)) * (col - n))
            end = col + len(s)
            self.text = self.text[:col] + s + self.text[end:]
            self.kinds[col:end] = bytes((k,)) * len(s)
        self.col = col + len(s)

    def _newline(self) -> None:
        self._finished.append(_runs(self.text, self.kinds))
        self.text = ""
        self.kinds = bytearray()
        self.col = 0
        self._dirty = True

    # -- handing changes to the view -------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._dirty

    def take(self) -> tuple[list[Runs], Runs]:
        """(lines finished since the last call, current active line). The first finished line
        replaces the line that was active at the previous call."""
        finished, self._finished = self._finished, []
        self._dirty = False
        return finished, _runs(self.text, self.kinds)
