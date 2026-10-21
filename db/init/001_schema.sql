-- Subscription-commerce warehouse used by the agents.
-- The agents never see this file: they learn what the tables mean from the ontology
-- and only see the columns the ontology says are relevant to a question.

CREATE TABLE customers (
    id          BIGINT PRIMARY KEY,
    email       TEXT        NOT NULL UNIQUE,
    full_name   TEXT        NOT NULL,
    country     CHAR(2)     NOT NULL,
    segment     TEXT        NOT NULL CHECK (segment IN ('consumer', 'business')),
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE products (
    id          BIGINT PRIMARY KEY,
    sku         TEXT          NOT NULL UNIQUE,
    name        TEXT          NOT NULL,
    brand       TEXT          NOT NULL,
    category    TEXT          NOT NULL,
    unit_price  NUMERIC(10,2) NOT NULL
);

CREATE TABLE subscriptions (
    id            BIGINT PRIMARY KEY,
    customer_id   BIGINT        NOT NULL REFERENCES customers (id),
    plan          TEXT          NOT NULL CHECK (plan IN ('basic', 'premium', 'family')),
    status        TEXT          NOT NULL CHECK (status IN ('active', 'paused', 'cancelled')),
    monthly_price NUMERIC(10,2) NOT NULL,
    started_at    DATE          NOT NULL,
    cancelled_at  DATE
);

CREATE TABLE orders (
    id              BIGINT PRIMARY KEY,
    customer_id     BIGINT        NOT NULL REFERENCES customers (id),
    subscription_id BIGINT        REFERENCES subscriptions (id),
    status          TEXT          NOT NULL CHECK (status IN ('paid', 'refunded', 'pending')),
    total_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ   NOT NULL
);

CREATE TABLE order_items (
    order_id   BIGINT        NOT NULL REFERENCES orders (id),
    product_id BIGINT        NOT NULL REFERENCES products (id),
    quantity   INT           NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(10,2) NOT NULL,
    PRIMARY KEY (order_id, product_id)
);

CREATE INDEX ON orders (customer_id);
CREATE INDEX ON orders (created_at);
CREATE INDEX ON subscriptions (customer_id);
CREATE INDEX ON order_items (product_id);

-- The agents connect as this role: read-only, with a hard statement timeout.
CREATE ROLE agent_reader LOGIN PASSWORD 'agent_reader';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_reader;
ALTER ROLE agent_reader SET statement_timeout = '5s';
ALTER ROLE agent_reader SET default_transaction_read_only = on;
