"""Create, once, what ucstate needs in the workspace but doesn't create itself.

    uv run python scripts/setup_workspace.py --profile <profile> \\
        --warehouse <warehouse id> --repo <owner>/<repo>
    uv run python scripts/setup_workspace.py --profile <profile> \\
        --warehouse <warehouse id> --repo <owner>/<repo> --github-secret

It creates only what is missing, and says what it found and did.

- **The target, CI shadow and rehearsal catalogs:** ucstate_dev, ucstate_prod,
  ucstate_shadow_pr, ucstate_shadow_main and ucstate_rehearsal. ucstate_shadow and ucstate_test
  are created on first use, locally.
- **The service principal** (`--principal`, default `ucstate-ci`), with the workspace-access and
  databricks-sql-access entitlements. Without them it can sign in but
  nothing else (Phase 0).
- **ALL PRIVILEGES and MANAGE for it on ucstate_prod, the two CI shadows and
  ucstate_rehearsal** (probes/phase3.py, q6), where an apply from main rehearses
  on clones of ucstate_prod's tables. It gets nothing else. It can't create
  catalogs, and it has no grant on ucstate_dev or ucstate_test.
- **With --github-vars,** the repository variables the workflows read:
  DATABRICKS_HOST (the profile's workspace), DATABRICKS_WAREHOUSE_ID (from
  --warehouse) and DATABRICKS_CLIENT_ID (the principal's application id).
  They are set with `gh variable set`. No secret is created or rotated.
- **With --github-secret,** those variables too, and a new OAuth secret for
  the principal, valid for a year. The secret goes straight to `gh secret
  set` on stdin, and is never printed or written to disk. The principal's
  older secrets are then deleted, so running it again rotates the secret.

Run locally, with `gh` signed in to an account that can write the repository's
settings. The warehouse runs the statements here, and CI uses the same one.
"""

from __future__ import annotations

import argparse
import subprocess

from databricks.sdk.service.iam import ComplexValue, ServicePrincipal

from ucmodel.sqltext import ident, string
from ucmodel.workspace import Workspace

CATALOGS = {
    "ucstate_dev": "ucstate target dev, applied locally",
    "ucstate_prod": "ucstate target prod, applied by GitHub Actions from main only",
    "ucstate_shadow_pr": "ucstate shadow for pull request plans in GitHub Actions",
    "ucstate_shadow_main": "ucstate shadow for applies from main in GitHub Actions",
    "ucstate_rehearsal": "ucstate rehearsal: shallow clones of a target's tables, "
                         "where a plan runs before the real apply",
}
CI_CATALOGS = ("ucstate_prod", "ucstate_shadow_pr", "ucstate_shadow_main", "ucstate_rehearsal")
CI_PRIVILEGES = ("ALL_PRIVILEGES", "MANAGE")
READ_CATALOGS = ("ucstate_prod",)
READ_PRIVILEGES = ("USE_SCHEMA", "SELECT")
SECRET_LIFETIME = f"{365 * 24 * 3600}s"


def catalogs(ws: Workspace) -> None:
    for name, comment in CATALOGS.items():
        if ws.catalog(name):
            print(f"catalog {name}: exists")
        else:
            ws.sql(f"CREATE CATALOG {ident(name)} COMMENT {string(comment)}")
            print(f"catalog {name}: created")


def principal(ws: Workspace, name: str) -> ServicePrincipal:
    w = ws.client
    found = list(w.service_principals.list(filter=f'displayName eq "{name}"'))
    if found:
        sp = found[0]
        print(f"service principal {name}: exists, application id {sp.application_id}")
    else:
        sp = w.service_principals.create(
            display_name=name, active=True,
            entitlements=[ComplexValue(value="workspace-access"),
                          ComplexValue(value="databricks-sql-access")])
        print(f"service principal {name}: created, application id {sp.application_id}")
    assert sp.id and sp.application_id
    for name in CI_CATALOGS:
        have = ws.grants("catalog", name).get(sp.application_id, [])
        for privilege in CI_PRIVILEGES:
            if privilege not in have:
                ws.sql(f"GRANT {privilege.replace('_', ' ')} ON CATALOG {ident(name)} "
                       f"TO `{sp.application_id}`")
                print(f"  granted {privilege} on {name}")
    return sp


def reader(ws: Workspace) -> None:
    """Whoever runs this can read what ucstate-ci creates."""
    me = ws.client.current_user.me().user_name
    assert me
    for name in READ_CATALOGS:
        have = ws.grants("catalog", name).get(me, [])
        for privilege in READ_PRIVILEGES:
            if privilege not in have:
                ws.sql(f"GRANT {privilege.replace('_', ' ')} ON CATALOG {ident(name)} "
                       f"TO {ident(me)}")
                print(f"  granted {privilege} on {name} to you")


def gh(*args: str, input: str | None = None) -> None:
    subprocess.run(["gh", *args], input=input, text=True, check=True, capture_output=True)


def github_vars(ws: Workspace, sp: ServicePrincipal, repo: str, warehouse: str) -> None:
    """The variables the workflows read. None of them is secret."""
    assert sp.application_id
    host = ws.client.config.host
    assert host
    values = {"DATABRICKS_HOST": host, "DATABRICKS_WAREHOUSE_ID": warehouse,
              "DATABRICKS_CLIENT_ID": sp.application_id}
    for name, value in values.items():
        gh("variable", "set", name, "--repo", repo, "--body", value)
    print(f"GitHub: {', '.join(values)} set on {repo}")


def github_secret(ws: Workspace, sp: ServicePrincipal, repo: str) -> None:
    """The secret exists only in this function and in GitHub. It is never printed."""
    assert sp.id and sp.application_id
    w = ws.client
    secret = w.service_principal_secrets_proxy.create(sp.id, lifetime=SECRET_LIFETIME)
    assert secret.secret and secret.id
    gh("secret", "set", "DATABRICKS_CLIENT_SECRET", "--repo", repo, input=secret.secret)
    print(f"GitHub: DATABRICKS_CLIENT_SECRET set (secret {secret.id}, expires "
          f"{secret.expire_time})")
    for old in w.service_principal_secrets_proxy.list(sp.id):
        if old.id and old.id != secret.id:
            w.service_principal_secrets_proxy.delete(sp.id, old.id)
            print(f"  deleted the older secret {old.id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", required=True,
                        help="the SQL warehouse that runs the statements, and CI's too")
    parser.add_argument("--repo", required=True, help="the GitHub repository, as owner/name")
    parser.add_argument("--principal", default="ucstate-ci",
                        help="the service principal CI signs in as (default: %(default)s)")
    parser.add_argument("--github-vars", action="store_true",
                        help="set the repository variables the workflows read")
    parser.add_argument("--github-secret", action="store_true",
                        help="create a new OAuth secret and store it in the repository, "
                             "with the variables")
    args = parser.parse_args()
    ws = Workspace.connect(args.profile, args.warehouse)
    catalogs(ws)
    sp = principal(ws, args.principal)
    reader(ws)
    if args.github_vars or args.github_secret:
        github_vars(ws, sp, args.repo, args.warehouse)
    if args.github_secret:
        github_secret(ws, sp, args.repo)


if __name__ == "__main__":
    main()
