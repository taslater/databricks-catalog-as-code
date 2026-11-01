"""Print schemas' raw REST state, scrubbed, as a sample (see `ucmodel.samples`)."""

import argparse
import json
import sys

from ucmodel import reader
from ucmodel.samples import anonymise
from ucmodel.workspace import Workspace

parser = argparse.ArgumentParser(prog="python -m ucmodel.samples")
parser.add_argument("--profile", required=True)
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True, action="append",
                    help="a schema to read. Give it once for each")
args = parser.parse_args()
raw = reader.read(Workspace.connect(args.profile), args.catalog, args.schema)
json.dump(anonymise(raw), sys.stdout, indent=1, sort_keys=True)
sys.stdout.write("\n")
