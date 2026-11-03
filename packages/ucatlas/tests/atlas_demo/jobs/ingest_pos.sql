INSERT INTO raw_pos.transactions
SELECT r.txn_id, r.store_id, r.txn_ts, r.loyalty_no, r.tender, r.total_gross
FROM read_files('/Volumes/landing/pos/transactions/', format => 'csv') AS r;

INSERT INTO raw_pos.transaction_lines
SELECT r.txn_id, r.line_no, r.sku, r.qty, r.unit_price, r.tax
FROM read_files('/Volumes/landing/pos/lines/', format => 'csv') AS r;

INSERT INTO raw_pos.refunds
SELECT r.refund_id, r.txn_id, r.refunded_at, r.amount
FROM read_files('/Volumes/landing/pos/refunds/', format => 'csv') AS r;
