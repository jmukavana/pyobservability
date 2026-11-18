# Security

Secure by default: one redactor sits on every path out of the process (logs, span data, metric
labels, OTel logs, audit payloads, captured headers). It **fails closed**: if redaction itself
errors, the value becomes `[REDACTION_FAILED]` and, for audit events, the event is flagged
`metadata._redaction_error`.

## What is redacted

| Rule | Examples |
|---|---|
| Secret **keys** | `password`, `token`, `api_key`, `apiKey`, `X-Api-Key`, `authorization`, `cookie`, `session`, `private_key`, … plus `extra_secret_keys` |
| Secret **values** in any text | JWTs, `Bearer`/`Basic` credentials, AWS/GitHub/Slack/Stripe keys, PEM private keys, `scheme://user:pass@`, `?access_token=…` query values, Luhn-valid card numbers, plus `extra_patterns` |
| PII | emails, `+`-prefixed phone numbers, IPv4 addresses, and fields named in `pii_fields` (`email`, `phone`, `first_name`, …) |

PII handling is `pii_strategy`: `mask` (`j***@example.com`), `hash` (salted HMAC, needs `hash_salt`,
keeps values correlatable) or `drop`. Nested dicts, lists, dataclasses and pydantic models are walked;
depth, size and cycles are bounded.

Never captured: request or response bodies, `Authorization`/`Cookie` headers, SQL bound parameters,
Celery task arguments. Request headers are captured only if allow-listed in `capture_headers`.
Raw URL paths are templated (`/claims/123` → `/claims/{id}`) for numeric, hex and UUID segments.

## The security lock

Environment variables and config files may **strengthen** security but never weaken it:

```text
$ PYOBS_SECURITY__MASK_SECRETS=false python app.py
SecurityPolicyError: environment variables may not weaken security settings ['mask_secrets'] …
```

- Weakening in code needs `security.allow_weakening=True`.
- The environment may weaken only with the deliberate grant `PYOBS_SECURITY__ALLOW_WEAKENING_FROM_ENV=true`.
- Config files can never weaken.

Weakening means: `redact_pii=False`, `mask_secrets=False`, `path_param_redaction=False`, adding to
`capture_headers`, removing from `pii_fields`, or enabling `capture_request_body` / `sql_parameters`.

!!! note "Reserved flags"
    `capture_request_body` and `sql_parameters` are locked now so the policy exists before any
    integration reads them, but **no integration captures bodies or SQL parameters today**; setting
    them changes nothing.

## Known limits

- Redaction rules are length-bounded so hostile input cannot stall the process: URL schemes up to 32
  characters, query-parameter names up to 64, PEM bodies up to 16 KiB, and email local parts up to
  64 characters. A longer prefix cannot hide a value, because the rules also match at every later
  position, but a name longer than the bound is not recognised as a secret key.

- `url.path` templating is a heuristic: `CLM-7` is not templated. Use `http.route` for grouping.
- The default PII field `address` also matches OpenTelemetry's `server.address`, so that attribute
  appears as `t***`. Remove it from `pii_fields` (a deliberate weakening) if you need it.
- Standard-library records are redacted on PyObservability's handlers; handlers you add yourself
  see the raw record.
- Redaction removes what it recognises. It cannot know that an arbitrary string is secret; use
  `extra_secret_keys` and `extra_patterns` for your own formats.
- The audit hash chain detects tampering; it does not prevent a database owner from deleting a
  tenant's *entire* tail or table. Anchor chain heads externally if that matters to you.

## Threat model summary

| Threat | Mitigation |
|---|---|
| Secrets or PII in logs, spans, metrics, exceptions | central redaction on every egress path, fail closed |
| Weakening masking through the environment | security lock |
| Request-ID header injection | incoming IDs must match `[A-Za-z0-9._-]{8,128}` or are replaced |
| Metric label explosion or leakage | label policy and per-metric cardinality cap |
| Unknown HTTP methods as labels | normalised to `_OTHER` |
| Audit event loss on crash or outage | durability boundary, no-drop queue, retry, dead letters, outbox |
| Audit tampering | append-only triggers, per-tenant HMAC hash chain, `verify()` |
| Two processes sharing one spool | `flock` on the spool directory |
| Audit recorded after `fork()` | refused with `AuditDurabilityError` |
| Credentials in config output | `SecretStr`; exporter options hidden from `repr` |
