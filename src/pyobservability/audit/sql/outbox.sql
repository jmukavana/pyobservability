CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.audit_outbox (
    id          bigserial   PRIMARY KEY,
    event_id    uuid        NOT NULL UNIQUE,
    payload     jsonb       NOT NULL,
    attempts    integer     NOT NULL DEFAULT 0,
    lease_until timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_outbox_claimable ON {schema}.audit_outbox (lease_until, id);
