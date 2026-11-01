CREATE VIEW probe_lineage.big_customers
AS SELECT customer_id, name, total FROM probe_lineage.customer_totals WHERE total > 5;
