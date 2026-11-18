CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.audit_events (
    event_id      uuid        PRIMARY KEY,
    chain_key     text        NOT NULL,
    seq           bigint      NOT NULL,
    prev_hash     text        NOT NULL,
    hash          text        NOT NULL,
    key_id        text,
    tenant_id     text,
    event_type    text        NOT NULL,
    category      text        NOT NULL,
    outcome       text        NOT NULL,
    actor_id      text,
    resource_type text        NOT NULL,
    resource_id   text        NOT NULL,
    action        text        NOT NULL,
    occurred_at   timestamptz NOT NULL,
    recorded_at   timestamptz NOT NULL,
    stored_at     timestamptz NOT NULL DEFAULT now(),
    payload       jsonb       NOT NULL,
    UNIQUE (chain_key, seq)
);

CREATE INDEX IF NOT EXISTS audit_events_tenant_time
    ON {schema}.audit_events (tenant_id, occurred_at);
CREATE INDEX IF NOT EXISTS audit_events_resource
    ON {schema}.audit_events (resource_type, resource_id);
CREATE INDEX IF NOT EXISTS audit_events_actor
    ON {schema}.audit_events (actor_id);

CREATE TABLE IF NOT EXISTS {schema}.audit_chain_heads (
    chain_key text   PRIMARY KEY,
    seq       bigint NOT NULL,
    hash      text   NOT NULL
);

CREATE OR REPLACE FUNCTION {schema}.audit_events_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP;
END
$$;

DROP TRIGGER IF EXISTS audit_events_append_only ON {schema}.audit_events;
CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON {schema}.audit_events
    FOR EACH ROW EXECUTE FUNCTION {schema}.audit_events_reject_change();

DROP TRIGGER IF EXISTS audit_events_no_truncate ON {schema}.audit_events;
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON {schema}.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION {schema}.audit_events_reject_change();
