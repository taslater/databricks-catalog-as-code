-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Lineage and constraint workload
-- MAGIC
-- MAGIC Synthetic objects that give lineage something to record and the ER
-- MAGIC diagrams declared keys to read. Everything lives in
-- MAGIC `workspace.probe_lineage`; the last cell drops it.

-- COMMAND ----------

CREATE SCHEMA IF NOT EXISTS workspace.probe_lineage
  COMMENT 'Synthetic objects for the capability probe; safe to drop'

-- COMMAND ----------

CREATE OR REPLACE TABLE workspace.probe_lineage.customers (
  customer_id BIGINT NOT NULL,
  name STRING,
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)

-- COMMAND ----------

CREATE OR REPLACE TABLE workspace.probe_lineage.orders (
  order_id BIGINT NOT NULL,
  customer_id BIGINT,
  amount DECIMAL(10, 2),
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customers_fk FOREIGN KEY (customer_id)
    REFERENCES workspace.probe_lineage.customers (customer_id)
)

-- COMMAND ----------

INSERT INTO workspace.probe_lineage.customers VALUES (1, 'Ada'), (2, 'Grace')

-- COMMAND ----------

INSERT INTO workspace.probe_lineage.orders VALUES (10, 1, 5.00), (11, 2, 7.50), (12, 1, 3.25)

-- COMMAND ----------

CREATE OR REPLACE TABLE workspace.probe_lineage.customer_totals AS
SELECT c.customer_id, c.name, sum(o.amount) AS total
FROM workspace.probe_lineage.customers AS c
JOIN workspace.probe_lineage.orders AS o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.name

-- COMMAND ----------

CREATE OR REPLACE VIEW workspace.probe_lineage.big_customers AS
SELECT customer_id, name, total
FROM workspace.probe_lineage.customer_totals
WHERE total > 5

-- COMMAND ----------

SELECT * FROM workspace.probe_lineage.big_customers

-- COMMAND ----------

-- Cleanup. Uncomment to drop everything above.
-- DROP SCHEMA workspace.probe_lineage CASCADE
