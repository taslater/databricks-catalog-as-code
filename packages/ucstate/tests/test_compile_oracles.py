"""`ucmodel.compiler` against models the platform built (docs/offline.md, section 3).

Each case compiles a source tree and compares the result with the model the
platform made of the same source, using `compare`, the planner's own
comparison. No differences means a plan can't tell them apart.
"""

from pathlib import Path

import pytest

from scenarios import all_scenarios
from ucmodel import samples
from ucmodel.compiler import Unsupported, compile_statements
from ucmodel.model import compare
from ucmodel.normalise import normalise
from ucmodel.sqltext import split_statements
from ucstate.project import Project, SourceFile
from ucstate.render import render

EXAMPLES = Path(__file__).parent.parent / "examples"


def compiled(files: list[SourceFile]) -> dict[str, dict]:
    return compile_statements((f.schema, s) for f in files for s in f.statements)


def test_the_round_trip_example_compiles_to_its_sample():
    files = Project.load(EXAMPLES / "roundtrip").sources()
    assert compare(compiled(files), samples.model("roundtrip")) == []


def test_the_oracle_sees_a_difference():
    """The known-bad control: a source changed after the sample was taken."""
    files = Project.load(EXAMPLES / "roundtrip").sources()
    for f in files:
        f.statements = [s.replace("'Contact address'", "'Email'") for s in f.statements]
    assert [str(d) for d in compare(compiled(files), samples.model("roundtrip"))] == [
        "table:roundtrip.customers columns.email.comment: desired 'Email', "
        "live 'Contact address'"]


@pytest.mark.parametrize("name", ["roundtrip", "shop", "probe_lineage"])
def test_each_sample_rendered_compiles_back_to_itself(name):
    model = samples.model(name)
    statements = []
    for path, text in sorted(render(model).items(), key=lambda kv: "schema.sql" not in kv[0]):
        schema = path.split("/")[1]
        statements += [(schema, s) for s in split_statements(text)]
    assert compare(compile_statements(statements), model) == []


@pytest.mark.parametrize("scenario", all_scenarios(), ids=lambda s: s.name)
def test_each_recording_compiles_from_its_after(scenario):
    recorded = scenario.recording()
    if recorded is None:
        pytest.skip("no recording yet: run it live once")
    if recorded["inputs"] != scenario.inputs():
        pytest.skip("the recording is stale: run it live again")
    try:
        model = compiled(scenario.sources("after"))
    except Unsupported as e:
        pytest.skip(f"declined: {str(e).splitlines()[0]}")
    assert compare(model, normalise(recorded["after"])) == []
