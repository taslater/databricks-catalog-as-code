"""Whether the live tests run, and how they authenticate.

Locally, UCSTATE_PROFILE names a Databricks CLI profile. In CI there is no
profile: UCSTATE_LIVE=1 runs them with the DATABRICKS_* variables instead,
which is what `Workspace.connect` does when it gets no profile. Either way they
need a warehouse, from UCSTATE_WAREHOUSE or DATABRICKS_WAREHOUSE_ID, and skip
without one.
"""

import os

PROFILE = os.environ.get("UCSTATE_PROFILE", "")
# The SQL warehouse the tests run on. There is no default: it is the workspace's.
WAREHOUSE = os.environ.get("UCSTATE_WAREHOUSE") or os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
WANTED = bool(PROFILE) or os.environ.get("UCSTATE_LIVE") == "1"
ENABLED = WANTED and bool(WAREHOUSE)
REASON = ("the live tests need a SQL warehouse: set UCSTATE_WAREHOUSE or DATABRICKS_WAREHOUSE_ID"
          if WANTED else
          "set UCSTATE_PROFILE, or UCSTATE_LIVE=1 with the DATABRICKS_* variables, "
          "to run the live tests")
