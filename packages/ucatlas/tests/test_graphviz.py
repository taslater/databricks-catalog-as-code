"""graphviz.py: where Graphviz's programs are found, and what a missing one says."""

import shutil
import sys

import pytest

from ucatlas import graphviz


def which(found: dict[str | None, str]):
    """A `shutil.which` that finds a program only in the folders `found` names
    (None for the PATH)."""
    return lambda program, path=None: found.get(path)


def test_a_program_on_the_path_is_used(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", which({None: "/usr/bin/dot",
                                                "/opt/homebrew/bin": "/opt/homebrew/bin/dot"}))
    assert graphviz.find("dot") == "/usr/bin/dot"


@pytest.mark.parametrize(("platform", "folder"), [
    ("darwin", "/opt/homebrew/bin"),
    ("darwin", "/opt/local/bin"),
    ("win32", r"C:\Program Files\Graphviz\bin"),
])
def test_a_program_off_the_path_is_found_where_installers_put_it(monkeypatch, platform, folder):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", which({folder: f"{folder}/dot"}))
    assert graphviz.find("dot") == f"{folder}/dot"


def test_a_program_in_no_folder_isnt_found(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", which({"/opt/homebrew/bin": "/opt/homebrew/bin/dot"}))
    assert graphviz.find("dot") is None


@pytest.mark.parametrize(("platform", "said"), [
    ("darwin", "`brew install graphviz`"),
    ("win32", "`winget install graphviz`"),
    ("linux", "`sudo apt-get install graphviz` (Debian, Ubuntu)"),
])
def test_the_message_says_how_to_install_it_here(monkeypatch, platform, said):
    monkeypatch.setattr(sys, "platform", platform)
    message = graphviz.missing("neato")
    assert message.startswith("Graphviz isn't installed, and ucatlas needs its `neato` program.")
    assert said in message
    assert "`conda install -c conda-forge graphviz`" in message
    assert "https://graphviz.org/download/" in message


def test_on_another_platform_the_message_still_says_how(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert "Install it with `conda install -c conda-forge graphviz`" in graphviz.missing("dot")
