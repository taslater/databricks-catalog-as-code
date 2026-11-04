"""progress.py: what a step writes, on a terminal and anywhere else."""

import io
import threading

import pytest

from ucmodel import progress
from ucmodel.progress import Progress


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(progress, "clock", c)
    return c


def test_off_writes_nothing():
    out = io.StringIO()
    p = Progress(out, enabled=False)
    with p.step("reading", 3, "columns") as s:
        s.tick(3)
    assert out.getvalue() == ""
    assert not Progress.off().live


def test_a_stream_that_isnt_a_terminal_gets_a_line_at_each_end(clock):
    out = io.StringIO()
    p = Progress(out)
    assert not p.live
    with p.step("reading column lineage", 2000, "columns") as s:
        for _ in range(2000):
            s.tick()
        clock.now += 812.4
    assert out.getvalue() == ("reading column lineage: 2,000 columns\n"
                              "reading column lineage: 2,000 done in 13 min 32 s\n")


@pytest.mark.parametrize(("n", "unit", "said"), [
    (1, "schemas", "1 schema"),
    (2, "schemas", "2 schemas"),
    (1, ("view or file", "views and files"), "1 view or file"),
    (3, ("view or file", "views and files"), "3 views and files"),
])
def test_a_count_is_singular_or_plural(n, unit, said):
    out = io.StringIO()
    with Progress(out).step("x", n, unit):
        pass
    assert out.getvalue().splitlines()[0] == f"x: {said}"


def test_a_step_with_no_unit_says_only_how_long_it_took(clock):
    out = io.StringIO()
    with Progress(out).step("writing the page"):
        clock.now += 0.25
    assert out.getvalue() == "writing the page\nwriting the page: 0.2 s\n"


def test_a_terminal_line_is_drawn_again_in_place_and_kept_at_the_end(clock):
    out = io.StringIO()
    p = Progress(out, live=True)
    with p.step("reading column lineage", 10, "columns") as s:
        clock.now += 1
        s.tick(4, detail="shop.orders")
        clock.now += 0.05  # sooner than a redraw
        s.tick()
        clock.now += 4
        s.tick()
    frames = out.getvalue().split("\r")[1:]
    assert frames[0].rstrip() == "reading column lineage: 0 of 10 columns (0%), 0.0 s"
    assert frames[1] == "reading column lineage: 4 of 10 columns (40%), shop.orders, 1.0 s"
    # the 5th item came too soon after the 4th to draw; the 6th is past ETA_AFTER
    assert frames[2] == ("reading column lineage: 6 of 10 columns (60%), shop.orders, 5.0 s, "
                         "about 3.4 s left")
    assert frames[3].rstrip() == "reading column lineage: 6 done in 5.0 s"
    assert out.getvalue().endswith("\n")
    assert len(frames) == 4


def test_a_shorter_line_covers_the_longer_one_before_it(clock):
    out = io.StringIO()
    p = Progress(out, live=True)
    with p.step("x", 2, "columns") as s:
        clock.now += 1
        s.tick(detail="a_very_long_schema_name.a_very_long_table_name")
    first, last = out.getvalue().rstrip("\n").split("\r")[-2:]
    assert len(last) == len(first)


def test_a_failing_step_on_a_terminal_ends_its_line_so_the_error_starts_on_its_own():
    out = io.StringIO()
    with pytest.raises(RuntimeError), Progress(out, live=True).step("laying out", 4, "tries"):
        raise RuntimeError("no dot")
    assert out.getvalue().endswith("\n")
    assert " done in " not in out.getvalue()


def test_a_failing_step_elsewhere_writes_no_end_line():
    out = io.StringIO()
    with pytest.raises(RuntimeError), Progress(out).step("laying out", 4, "tries"):
        raise RuntimeError("no dot")
    assert out.getvalue() == "laying out: 4 tries\n"


def test_ticks_from_many_threads_all_count():
    out = io.StringIO()
    with Progress(out).step("reading", 8000, "objects") as s:
        threads = [threading.Thread(target=lambda: [s.tick() for _ in range(1000)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert s.done == 8000
