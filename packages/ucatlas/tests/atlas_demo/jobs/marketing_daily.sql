INSERT OVERWRITE marketing.campaign_attribution
SELECT s.campaign_code, o.order_key, sum(f.net_amount) AS attributed_revenue
FROM raw_web.sessions AS s
JOIN core.customers AS c ON c.web_account_id = s.account_id
JOIN core.orders AS o ON o.customer_id = c.customer_id
  AND o.ordered_at BETWEEN s.started_at AND s.started_at + INTERVAL 7 DAYS
JOIN sales.order_facts AS f ON f.order_key = o.order_key
WHERE s.campaign_code IS NOT NULL
GROUP BY s.campaign_code, o.order_key;

INSERT OVERWRITE marketing.customer_list
SELECT c.contact_id AS customer_id, c.region, c.created_at AS first_seen, c.loyalty_no,
  c.email_hash
FROM raw_crm.contacts AS c
LEFT JOIN raw_web.accounts AS a ON a.email_hash = c.email_hash;
