-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Free Edition capability probe
-- MAGIC
-- MAGIC Read-only. Run each cell on serverless compute. A cell that fails is an
-- MAGIC answer, not a bug: record it under "Open questions" in `AGENTS.md`.

-- COMMAND ----------

SELECT
  current_metastore() AS metastore,
  current_catalog() AS catalog,
  current_schema() AS schema,
  current_user() AS user,
  current_version() AS version

-- COMMAND ----------

SHOW CATALOGS

-- COMMAND ----------

SHOW SCHEMAS IN system

-- COMMAND ----------

-- Lineage, for goal 2. Unverified on Free Edition.
SELECT * FROM system.access.table_lineage ORDER BY event_time DESC LIMIT 20

-- COMMAND ----------

SELECT * FROM system.access.column_lineage ORDER BY event_time DESC LIMIT 20

-- COMMAND ----------

-- Declared keys, for the ER diagrams and the desired-state diff.
SELECT * FROM system.information_schema.table_constraints LIMIT 20

-- COMMAND ----------

SELECT * FROM system.information_schema.referential_constraints LIMIT 20
