"""Finding Graphviz's programs, and saying how to install them when they're missing.

`ucatlas` runs `dot` (and `neato`, for the explorer) as programs, so Graphviz
is installed apart from the Python packages: pip and uv can't install it.
A program is looked for on the PATH, then in the folders installers put it
in. A terminal or editor started before the install, or a PATH without
Homebrew's folder, still finds it there.
"""

from __future__ import annotations

import shutil
import sys

FOLDERS = {
    "darwin": ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"),
    "win32": (r"C:\Program Files\Graphviz\bin", r"C:\Program Files (x86)\Graphviz\bin"),
}

INSTALL = {
    "darwin": "`brew install graphviz`",
    "win32": "`winget install graphviz`",
    "linux": "`sudo apt-get install graphviz` (Debian, Ubuntu) "
             "or `sudo dnf install graphviz` (Fedora)",
}


def find(program: str) -> str | None:
    """The path of one of Graphviz's programs, or None when it isn't installed."""
    found = shutil.which(program)
    for folder in FOLDERS.get(sys.platform, ()):
        found = found or shutil.which(program, path=folder)
    return found


def missing(program: str) -> str:
    """What to say when `program` can't be found: how to install Graphviz here."""
    how = INSTALL.get(sys.platform)
    ways = f"{how}, or with " if how else ""
    return (f"Graphviz isn't installed, and ucatlas needs its `{program}` program. "
            f"Install it with {ways}`conda install -c conda-forge graphviz`, then try again "
            f"in a new terminal. Other ways: https://graphviz.org/download/")
