# type: ignore
# The probe stays exactly as it ran, so type checkers skip it, as ruff does.
"""A job that writes `ucstate_test.kinds` from two places query history tells apart.

The explorer reads, from a snapshot taken with a warehouse, the statements query
history says wrote each object, and where each ran from. This job gives it one of
each source the CLI can't: a notebook on serverless compute, and a SQL file run on
the warehouse by a job task.

- `daily` (a notebook, serverless) rebuilds `kinds.order_daily`, revenue and orders
  per day and region, from `kinds.orders`.
- `refresh` (a SQL file, the warehouse) merges the day's order counts into it.

Every object is synthetic. Running it again updates the notebook, the file and the
job in place, then runs the job once and waits for it:

    uv run python -m probes.history_job --profile P --warehouse W

The job and its files stay in the workspace, under the caller's home folder, in
`ucatlas-history-probe/`. It creates one table, `kinds.order_daily`.
"""

import argparse
import base64

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import jobs
from databricks.sdk.service.workspace import ImportFormat, Language

NAME = "ucatlas history probe"

DAILY = """-- Databricks notebook source
CREATE OR REPLACE TABLE ucstate_test.kinds.order_daily
COMMENT 'Revenue and orders per day and region, rebuilt by the history probe job'
AS SELECT date(placed_at) AS day, region, sum(amount) AS revenue, count(*) AS orders
FROM ucstate_test.kinds.orders
WHERE placed_at IS NOT NULL
GROUP BY ALL
"""

REFRESH = """MERGE INTO ucstate_test.kinds.order_daily AS d
USING (
  SELECT date(placed_at) AS day, region, count(*) AS orders
  FROM ucstate_test.kinds.orders
  WHERE placed_at IS NOT NULL
  GROUP BY ALL
) AS s
ON d.day = s.day AND d.region = s.region
WHEN MATCHED THEN UPDATE SET d.orders = s.orders
WHEN NOT MATCHED THEN INSERT (day, region, orders) VALUES (s.day, s.region, s.orders)
"""


def upload(w: WorkspaceClient, path: str, text: str, notebook: bool) -> None:
    content = base64.b64encode(text.encode()).decode()
    if notebook:
        w.workspace.import_(path, content=content, format=ImportFormat.SOURCE,
                            language=Language.SQL, overwrite=True)
    else:
        w.workspace.import_(path, content=content, format=ImportFormat.RAW, overwrite=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m probes.history_job")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", required=True)
    args = parser.parse_args()
    w = WorkspaceClient(profile=args.profile)
    folder = f"/Users/{w.current_user.me().user_name}/ucatlas-history-probe"
    w.workspace.mkdirs(folder)
    upload(w, f"{folder}/daily", DAILY, notebook=True)
    upload(w, f"{folder}/refresh.sql", REFRESH, notebook=False)
    settings = dict(
        name=NAME,
        tasks=[
            jobs.Task(task_key="daily", notebook_task=jobs.NotebookTask(
                notebook_path=f"{folder}/daily", warehouse_id=None)),
            jobs.Task(task_key="refresh", depends_on=[jobs.TaskDependency("daily")],
                      sql_task=jobs.SqlTask(warehouse_id=args.warehouse, file=jobs.SqlTaskFile(
                          path=f"{folder}/refresh.sql", source=jobs.Source.WORKSPACE))),
        ],
        max_concurrent_runs=1,
    )
    found = [j for j in w.jobs.list(name=NAME)]
    if found:
        job_id = found[0].job_id
        w.jobs.reset(job_id, jobs.JobSettings(**settings))
    else:
        job_id = w.jobs.create(**settings).job_id
    print(f"job {job_id}: running")
    run = w.jobs.run_now_and_wait(job_id)
    for task in run.tasks or []:
        state = task.state
        print(f"  {task.task_key}: {state.result_state if state else '?'}"
              f"{' - ' + state.state_message if state and state.state_message else ''}")
    print(f"run {run.run_id}: {run.state.result_state if run.state else '?'}")


if __name__ == "__main__":
    main()
