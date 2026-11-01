# Security policy

## Reporting a vulnerability

Please report a vulnerability privately. Don't open a public issue or pull
request for it.

Use GitHub's private vulnerability reporting: on the repository's page, open
the **Security** tab and choose **Report a vulnerability**. Say which package
and version is affected, how to reproduce the problem, and what you think the
impact is.

We aim to reply within a week. We will confirm the problem, work on a
fix, and tell you when a release is out. We credit reporters who want credit.

## Scope

`ucmodel`, `ucstate` and `ucatlas` read and change a Databricks workspace with
the credentials you give them. These are the problems that count:

- a command that prints or stores a secret or token;
- a command that uses credentials or a Databricks CLI profile you didn't name;
- a way to make `ucstate apply` run SQL that is not in the plan;
- a file-writing path outside the folder you gave `ucstate import` or
  `ucatlas draw`.

A plan that is wrong for your catalog is a bug, not a vulnerability. Report it
as a normal issue, and don't include data or SQL you may not publish.

## Handling credentials

Pass credentials as a named CLI profile or as `DATABRICKS_*` environment
variables. In CI, keep the client secret in the repository's secrets and never
in a variable or a file. A secret that has appeared in a log, issue or commit
should be treated as leaked and rotated.

## Supported versions

This is alpha software. Only the latest release gets fixes.
