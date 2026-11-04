"""How far a long step has got, written to stderr.

Reading a catalog takes several REST calls per object and, with lineage, one
per column, and laying out a big explorer runs Graphviz many times. Each of
those is a step: something with a label and, when it is known, how many
items it has. How a step shows depends on where the output goes:

- **On a terminal**, one line is drawn again in place as items finish, with
  how many are done, the share, the time so far and, once there is a rate to
  go by, about how long is left.
- **Anywhere else** (a pipe, a CI log, the output `ucatlas serve` shows),
  a step writes one line when it starts and one when it ends, with how long
  it took. A log shows where the time went without a line per item.

`Progress.off()` writes nothing. The library functions take a `progress`
argument and default to it, so only the command line writes progress, and
`--quiet` turns it off there too. Progress never goes to stdout, which keeps
what a command prints there (a snapshot's path, say) for programs.

Items may finish on several threads at once, as `Workspace.map` runs them.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

REDRAW = 0.1  # seconds between redraws of a terminal line
ETA_AFTER = 3.0  # seconds before guessing what is left: the first items are no guide
clock = time.monotonic  # a test sets its own


Unit = str | tuple[str, str]


def _plural(n: int, unit: Unit) -> str:
    """`1 schema`, `2 schemas`. A unit is its plural, made singular by dropping an `s`,
    or (singular, plural) for one that isn't, as in ("view or file", "views and files")."""
    one, many = unit if isinstance(unit, tuple) else (unit.removesuffix("s"), unit)
    return f"{n:,} {one if n == 1 else many}".rstrip()


def _seconds(s: float) -> str:
    if s < 60:
        return f"{s:.1f} s"
    m, s = divmod(round(s), 60)
    return f"{m} min {s:02d} s" if m < 60 else f"{m // 60} h {m % 60:02d} min"


class Step:
    """One step under way. `tick` when an item finishes, from any thread."""

    def __init__(self, progress: Progress, label: str, total: int | None, unit: Unit):
        self.progress, self.label, self.total, self.unit = progress, label, total, unit
        self.done = 0
        self.detail = ""
        self.start = clock()
        self._drawn = 0.0
        self._lock = threading.Lock()

    def tick(self, n: int = 1, detail: str | None = None) -> None:
        """`n` more items have finished. `detail`, if given, names what is under way
        now (a schema, a layout) on a terminal's line."""
        with self._lock:
            self.done += n
            if detail is not None:
                self.detail = detail
            now = clock()
            if self.progress.live and now - self._drawn >= REDRAW:
                self._drawn = now
                self.progress._draw(self.line(now))

    def line(self, now: float | None = None) -> str:
        """The terminal line: how many are done, the share, the time so far and what
        is left."""
        elapsed = (now or clock()) - self.start
        if self.total:
            share = f" ({100 * self.done // self.total}%)"
            parts = [f"{self.label}: {self.done:,} of {_plural(self.total, self.unit)}{share}"]
        else:
            parts = [f"{self.label}: {_plural(self.done, self.unit)}" if self.unit
                     else self.label]
        if self.detail:
            parts.append(self.detail)
        parts.append(_seconds(elapsed))
        if self.total and self.done and elapsed >= ETA_AFTER and self.done < self.total:
            left = elapsed / self.done * (self.total - self.done)
            parts.append(f"about {_seconds(left)} left")
        return ", ".join(parts)

    def started(self) -> str:
        return f"{self.label}: {_plural(self.total, self.unit)}" if self.total is not None \
            and self.unit else self.label

    def ended(self) -> str:
        took = _seconds(clock() - self.start)
        counted = f"{self.done:,} done in " if self.unit else ""
        return f"{self.label}: {counted}{took}"


class Progress:
    """Where progress goes: `stream` (stderr by default), drawn in place when it is a
    terminal, or as start and end lines when it isn't. `live` overrides the test."""

    def __init__(self, stream: TextIO | None = None, *, live: bool | None = None,
                 enabled: bool = True):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        isatty = getattr(self.stream, "isatty", None)
        self.live = enabled and (bool(isatty and isatty()) if live is None else live)
        self._width = 0

    @classmethod
    def off(cls) -> Progress:
        return cls(enabled=False)

    @contextmanager
    def step(self, label: str, total: int | None = None, unit: Unit = "") -> Iterator[Step]:
        """A step with `total` items counted in `unit` (plural, as in `columns`), or
        with no count when `unit` is empty. On a terminal its line stays when it ends,
        with how long it took; a step that fails ends its line so the error starts on
        its own."""
        s = Step(self, label, total, unit)
        if self.enabled:
            if self.live:
                self._draw(s.line())
            else:
                self._write(s.started())
        try:
            yield s
        except BaseException:
            if self.live and self._width:
                self.stream.write("\n")
                self._width = 0
            raise
        if self.enabled:
            if self.live:
                self._draw(s.ended())
                self.stream.write("\n")
                self.stream.flush()
                self._width = 0
            else:
                self._write(s.ended())

    def _write(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def _draw(self, text: str) -> None:
        # pad to the last line's width rather than use an escape code, which some
        # terminals (and editors' output panes) print as text
        pad = max(0, self._width - len(text))
        self.stream.write("\r" + text + " " * pad)
        self.stream.flush()
        self._width = len(text)
