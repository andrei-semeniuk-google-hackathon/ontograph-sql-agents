-- Deterministic seed data (no random()): evaluation answers stay stable between runs.

INSERT INTO customers (id, email, full_name, country, segment, created_at)
SELECT g,
       'customer' || g || '@example.com',
       'Customer ' || g,
       (ARRAY['US', 'GB', 'DE', 'FR', 'CY', 'CA'])[1 + g % 6],
       CASE WHEN g % 10 = 0 THEN 'business' ELSE 'consumer' END,
       TIMESTAMPTZ '2024-06-01 00:00:00+00' + make_interval(days => (g * 3) % 500)
FROM generate_series(1, 500) AS g;

INSERT INTO products (id, sku, name, brand, category, unit_price)
SELECT g,
       'SKU-' || lpad(g::text, 4, '0'),
       (ARRAY['Amber', 'Cedar', 'Citrus', 'Iris', 'Musk', 'Oud', 'Rose', 'Vetiver'])[1 + g % 8]
           || ' ' || (ARRAY['Eau de Parfum', 'Candle', 'Body Lotion', 'Discovery Set'])[1 + g % 4],
       (ARRAY['Maison Nord', 'Atelier Sud', 'Kyoto Lab', 'Verde', 'Lumen'])[1 + g % 5],
       (ARRAY['fragrance', 'candle', 'body care', 'gift set'])[1 + g % 4],
       (ARRAY[29.00, 24.50, 18.00, 45.00])[1 + g % 4] + (g % 5) * 2
FROM generate_series(1, 40) AS g;

INSERT INTO subscriptions (id, customer_id, plan, status, monthly_price, started_at, cancelled_at)
SELECT g,
       g,
       (ARRAY['basic', 'premium', 'family'])[1 + g % 3],
       CASE WHEN g % 7 = 0 THEN 'cancelled' WHEN g % 11 = 0 THEN 'paused' ELSE 'active' END,
       (ARRAY[16.95, 24.95, 34.95])[1 + g % 3],
       DATE '2025-01-01' + (g * 3) % 540,
       CASE WHEN g % 7 = 0 THEN DATE '2025-01-01' + (g * 3) % 540 + 90 END
FROM generate_series(1, 500) AS g
WHERE g % 5 <> 0;

INSERT INTO orders (id, customer_id, subscription_id, status, created_at)
SELECT g,
       1 + g % 500,
       CASE WHEN (1 + g % 500) % 5 <> 0 THEN 1 + g % 500 END,
       CASE WHEN g % 20 = 0 THEN 'refunded' WHEN g % 33 = 0 THEN 'pending' ELSE 'paid' END,
       TIMESTAMPTZ '2025-01-01 10:00:00+00' + make_interval(days => (g * 7) % 640, hours => g % 12)
FROM generate_series(1, 4000) AS g;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT o.id, p.product_id, 1 + (o.id + p.n) % 3, pr.unit_price
FROM orders o
CROSS JOIN LATERAL (
    SELECT n, 1 + (o.id * 7 + n * 13) % 40 AS product_id
    FROM generate_series(0, o.id % 3) AS n
) p
JOIN products pr ON pr.id = p.product_id;

UPDATE orders o
SET total_amount = t.total
FROM (SELECT order_id, SUM(quantity * unit_price) AS total FROM order_items GROUP BY order_id) t
WHERE t.order_id = o.id;

ANALYZE;
