CREATE TABLE probe_lineage.customer_totals (
  customer_id BIGINT,
  name STRING,
  total DECIMAL(20,2)
)
CLUSTER BY AUTO;
