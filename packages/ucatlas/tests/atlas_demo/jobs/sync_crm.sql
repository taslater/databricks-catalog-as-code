MERGE INTO raw_crm.contacts AS t
USING (
  SELECT r.contact_id, r.loyalty_no, r.web_account_id, r.full_name, r.email_hash, r.region,
    r.created_at
  FROM read_files('/Volumes/landing/crm/contacts/', format => 'parquet') AS r
) AS s
ON t.contact_id = s.contact_id
WHEN MATCHED THEN UPDATE SET t.loyalty_no = s.loyalty_no, t.web_account_id = s.web_account_id,
  t.full_name = s.full_name, t.email_hash = s.email_hash, t.region = s.region
WHEN NOT MATCHED THEN INSERT (contact_id, loyalty_no, web_account_id, full_name, email_hash,
  region, created_at)
  VALUES (s.contact_id, s.loyalty_no, s.web_account_id, s.full_name, s.email_hash, s.region,
  s.created_at);
