INSERT INTO raw_web.orders
SELECT r.order_id, r.account_id, r.placed_at, r.currency, r.status, r.payload
FROM read_files('/Volumes/landing/web/orders/', format => 'json') AS r;

INSERT INTO raw_web.order_items
SELECT r.order_id, r.item_no, r.sku, r.qty, r.price, r.discount
FROM read_files('/Volumes/landing/web/order_items/', format => 'json') AS r;

INSERT INTO raw_web.accounts
SELECT r.account_id, r.email_hash, r.signup_at, r.country
FROM read_files('/Volumes/landing/web/accounts/', format => 'json') AS r;

INSERT INTO raw_web.sessions
SELECT r.session_id, r.account_id, r.started_at, r.campaign_code, r.landing_page
FROM read_files('/Volumes/landing/web/sessions/', format => 'json') AS r;
