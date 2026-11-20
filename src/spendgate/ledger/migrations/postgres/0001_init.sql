-- spendgate ledger schema, PostgreSQL dialect. Mirrors docs/data-model.md; change both together.
CREATE TABLE ledger_meta (
  key   TEXT PRIMARY KEY,                            -- 'currency': the ledger currency, written on first open
  value TEXT NOT NULL
);

CREATE TABLE scopes (
  id               TEXT    PRIMARY KEY,                -- ULID
  kind             TEXT    NOT NULL CHECK (kind IN ('org','project','service','user','session')),
  parent_id        TEXT    REFERENCES scopes(id),
  name             TEXT    NOT NULL,                   -- segment value, e.g. 'api'
  path             TEXT    NOT NULL UNIQUE,            -- human path, e.g. 'org:acme/project:app/service:api'; a session segment is 'session:<key id>:<session id>' and may contain ':' and '#' (L-b, L-k)
  created_at       BIGINT  NOT NULL,
  disabled_at      BIGINT,
  -- session scopes only; copied from keys.session_defaults when the session is created (NULL/0 for other kinds)
  max_steps        BIGINT  CHECK (max_steps IS NULL OR max_steps > 0),
  max_seconds      BIGINT  CHECK (max_seconds IS NULL OR max_seconds > 0),
  idle_seconds     BIGINT  CHECK (idle_seconds IS NULL OR idle_seconds > 0),
  step_count       BIGINT  NOT NULL DEFAULT 0 CHECK (step_count >= 0),
  started_at       BIGINT,
  last_activity_at BIGINT,
  closed_at        BIGINT,
  closed_reason    TEXT    CHECK (closed_reason IS NULL OR closed_reason IN ('steps','time','budget')),  -- why a limit closed it; NULL while open and for an idle restart; set together with closed_at
  CHECK (closed_reason IS NULL OR closed_at IS NOT NULL),
  CHECK ((kind = 'org') = (parent_id IS NULL)),
  CHECK (kind = 'session' OR (max_steps IS NULL AND max_seconds IS NULL AND idle_seconds IS NULL
                              AND started_at IS NULL AND step_count = 0)),
  UNIQUE (parent_id, name)
);

CREATE TABLE keys (
  id             TEXT    PRIMARY KEY,
  prefix         TEXT    NOT NULL UNIQUE,            -- the <prefix> in sg-<prefix>-<secret>
  secret_hash    TEXT    NOT NULL,                   -- hex SHA-256(secret || server pepper); never the secret
  kind           TEXT    NOT NULL DEFAULT 'api' CHECK (kind IN ('api','admin')),
  scope_id       TEXT    NOT NULL REFERENCES scopes(id),
  name           TEXT    NOT NULL,
  allowed_models TEXT,                               -- JSON array of globs; NULL = all
  limits         TEXT,                               -- JSON: rpm, tpm, max_concurrent, max_output_tokens, per_call_ceiling_nano
  session_defaults TEXT,                             -- JSON: budget_nano, max_steps, max_seconds, idle_seconds, max_new_sessions_per_hour; NULL = sessions not allowed
  admin_perms    TEXT,                               -- JSON array, admin keys only
  created_at     BIGINT  NOT NULL,
  expires_at     BIGINT,                             -- rotation sets this on the old key (grace period)
  revoked_at     BIGINT,
  replaces_key_id TEXT   REFERENCES keys(id)
);

CREATE TABLE budgets (
  id                 TEXT    PRIMARY KEY,
  scope_id           TEXT    NOT NULL REFERENCES scopes(id),
  period             TEXT    NOT NULL CHECK (period IN ('none','day','month')),   -- UTC
  cap_nano           BIGINT  NOT NULL CHECK (cap_nano >= 0),
  soft_pct           INTEGER NOT NULL DEFAULT 80 CHECK (soft_pct BETWEEN 1 AND 99),
  model_filter       TEXT,                           -- glob; NULL = every model
  strict_overrun     INTEGER NOT NULL DEFAULT 1 CHECK (strict_overrun IN (0,1)),
  review_required_at BIGINT,                         -- set when a breach overrun happened; cleared only by ack-overrun
  note               TEXT,                           -- free text from the admin API (max 500)
  created_at         BIGINT  NOT NULL,
  disabled_at        BIGINT
);
-- Additive grants: effective cap = cap_nano + SUM(amount_nano) of grants with valid_from <= now < valid_until.
CREATE TABLE budget_grants (
  id           TEXT   PRIMARY KEY,
  budget_id    TEXT   NOT NULL REFERENCES budgets(id),
  amount_nano  BIGINT NOT NULL CHECK (amount_nano > 0),
  valid_from   BIGINT NOT NULL,
  valid_until  BIGINT NOT NULL,
  created_by   TEXT   NOT NULL,
  created_at   BIGINT NOT NULL,
  reason       TEXT   NOT NULL,
  CHECK (valid_until > valid_from)
);
CREATE INDEX budget_grants_budget ON budget_grants(budget_id, valid_until);
CREATE INDEX budgets_scope ON budgets(scope_id) WHERE disabled_at IS NULL;

-- Money state. One row per budget and period start; every access happens under that budget's row lock.
CREATE TABLE budget_periods (
  budget_id    TEXT   NOT NULL REFERENCES budgets(id),
  period_start BIGINT NOT NULL,                      -- epoch ms UTC; 0 for period 'none'
  spent_nano   BIGINT NOT NULL DEFAULT 0 CHECK (spent_nano >= 0),
  held_nano    BIGINT NOT NULL DEFAULT 0 CHECK (held_nano >= 0),
  overrun_nano BIGINT NOT NULL DEFAULT 0 CHECK (overrun_nano >= 0),  -- sum of (charge - hold) over overrunning calls
  excess_nano  BIGINT NOT NULL DEFAULT 0 CHECK (excess_nano >= 0),   -- spent+held above the effective cap, recorded when a cap is lowered or a grant expires
  calls_count  BIGINT NOT NULL DEFAULT 0 CHECK (calls_count >= 0),
  soft_alerted_at BIGINT,
  PRIMARY KEY (budget_id, period_start)
);

CREATE TABLE reservations (
  id               TEXT   PRIMARY KEY,               -- request id, unique for the retention window
  state            TEXT   NOT NULL CHECK (state IN ('pending','settled','released','expired')),   -- reservation states, not call outcomes
  owner_instance   TEXT   NOT NULL,
  lease_expires_at BIGINT NOT NULL,
  key_id           TEXT   NOT NULL REFERENCES keys(id),
  scope_id         TEXT   NOT NULL REFERENCES scopes(id),   -- leaf scope of the chain
  session_id       TEXT   REFERENCES scopes(id),            -- counted step, if any
  outcome          TEXT   CHECK (outcome IS NULL OR outcome IN ('ok','ok_no_usage','client_error','throttled',
                     'timeout','upstream_5xx','unreachable','cancelled','stream_broken','internal_error','expired')),
  charged_nano     BIGINT CHECK (charged_nano IS NULL OR charged_nano >= 0),
  meta             TEXT   NOT NULL,                         -- JSON written at hold time: endpoint, model, candidates (provider, deployment, price_id, fx), catalog_sha, labels; lets the sweeper write a complete calls row
  created_at       BIGINT NOT NULL,
  finished_at      BIGINT,
  CHECK ((state = 'pending') = (finished_at IS NULL))
);
CREATE INDEX reservations_lease ON reservations(lease_expires_at) WHERE state = 'pending';
CREATE INDEX reservations_owner ON reservations(owner_instance)   WHERE state = 'pending';

CREATE TABLE reservation_items (
  reservation_id TEXT   NOT NULL REFERENCES reservations(id),
  budget_id      TEXT   NOT NULL REFERENCES budgets(id),
  period_start   BIGINT NOT NULL,                    -- the period in which the hold was made; settlement goes here
  held_nano      BIGINT NOT NULL CHECK (held_nano >= 0),
  overrun_nano   BIGINT NOT NULL DEFAULT 0 CHECK (overrun_nano >= 0),
  PRIMARY KEY (reservation_id, budget_id)
);

-- One row per provider attempt; a request failed over to another deployment of the same catalog id has attempts 1..n
-- under ONE reservation. Refused requests write no row. A late settle after expiry UPDATEs the sweeper's row.
CREATE TABLE calls (
  request_id          TEXT   NOT NULL,
  attempt             INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  key_id              TEXT   NOT NULL,
  scope_id            TEXT   NOT NULL,
  session_id          TEXT,
  user_label          TEXT,                          -- X-Spendgate-User or fallback; attribution only
  labels              TEXT,                          -- JSON object, tags for usage grouping
  endpoint            TEXT   NOT NULL,               -- e.g. /v1/chat/completions
  provider            TEXT   NOT NULL,               -- slug = module name
  deployment          TEXT   NOT NULL,
  model               TEXT   NOT NULL,               -- catalog id
  price_id            TEXT   NOT NULL,               -- catalog entry id + effective_from used
  catalog_sha         TEXT   NOT NULL,               -- sha256 of the catalog files loaded
  tokens_in           BIGINT NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out          BIGINT NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  tokens_cached       BIGINT NOT NULL DEFAULT 0 CHECK (tokens_cached >= 0),
  tokens_cache_write  BIGINT NOT NULL DEFAULT 0 CHECK (tokens_cache_write >= 0),
  tokens_reasoning    BIGINT NOT NULL DEFAULT 0 CHECK (tokens_reasoning >= 0),
  audio_tokens_in     BIGINT NOT NULL DEFAULT 0 CHECK (audio_tokens_in >= 0),
  audio_tokens_out    BIGINT NOT NULL DEFAULT 0 CHECK (audio_tokens_out >= 0),
  audio_ms            BIGINT NOT NULL DEFAULT 0 CHECK (audio_ms >= 0),
  characters          BIGINT NOT NULL DEFAULT 0 CHECK (characters >= 0),
  images              BIGINT NOT NULL DEFAULT 0 CHECK (images >= 0),
  native_currency     TEXT   NOT NULL,
  native_cost_nano    BIGINT NOT NULL DEFAULT 0 CHECK (native_cost_nano >= 0),   -- native currency nano-units
  fx_rate_nano        BIGINT NOT NULL CHECK (fx_rate_nano > 0),                  -- ledger per native x 1e9; 1000000000 when currencies match
  charged_nano        BIGINT NOT NULL DEFAULT 0 CHECK (charged_nano >= 0),       -- ledger currency: money booked to the budgets for this attempt (0 if not_processed, full hold for cancel/5xx after send or sweeper expiry); sums to the settle charge
  usage_cost_nano     BIGINT CHECK (usage_cost_nano IS NULL OR usage_cost_nano >= 0),  -- ledger currency, ceiling, computed from reported usage; NULL when no usage was seen
  hold_nano           BIGINT NOT NULL DEFAULT 0 CHECK (hold_nano >= 0),
  overrun_nano        BIGINT NOT NULL DEFAULT 0 CHECK (overrun_nano >= 0),
  http_status         INTEGER,
  outcome             TEXT   NOT NULL CHECK (outcome IN ('ok','ok_no_usage','client_error','throttled',
                        'timeout','upstream_5xx','unreachable','cancelled','stream_broken','internal_error','expired')),
  error_code          TEXT,
  streamed            INTEGER NOT NULL DEFAULT 0 CHECK (streamed IN (0,1)),
  settled_by          TEXT   NOT NULL DEFAULT 'owner' CHECK (settled_by IN ('owner','sweeper')),
  review_required_at  BIGINT,                        -- set by the C-22 usage sanity flag (the usage is still booked in full)
  started_at          BIGINT NOT NULL,
  finished_at         BIGINT NOT NULL,
  latency_ms          BIGINT,
  ttfb_ms             BIGINT,
  PRIMARY KEY (request_id, attempt)
);
CREATE INDEX calls_finished     ON calls(finished_at);
CREATE INDEX calls_key_finished ON calls(key_id, finished_at);
CREATE INDEX calls_model_finished ON calls(model, finished_at);

CREATE TABLE kill_switches (
  id          TEXT   PRIMARY KEY,
  target_kind TEXT   NOT NULL CHECK (target_kind IN ('global','scope','key','provider','model')),
  target_ref  TEXT   NOT NULL DEFAULT '',            -- '' for global; scope ULID, key id, provider slug, catalog model id
  reason      TEXT   NOT NULL,
  created_by  TEXT   NOT NULL,
  created_at  BIGINT NOT NULL,
  cleared_at  BIGINT,
  CHECK ((target_kind = 'global') = (target_ref = ''))
);
CREATE INDEX kill_active ON kill_switches(target_kind, target_ref) WHERE cleared_at IS NULL;

-- Append-only, protected by triggers (no hash chain, no advisory lock). Rows are written in the same transaction as the change.
CREATE TABLE admin_audit (
  id          TEXT   PRIMARY KEY,                    -- ULID
  at          BIGINT NOT NULL,
  actor_key_id TEXT  NOT NULL,
  action      TEXT   NOT NULL,                       -- budget.set_cap, budget.grant, key.revoke, overrun.ack, kill.set ...
  target_type TEXT   NOT NULL,
  target_id   TEXT   NOT NULL,
  request_id  TEXT,                                  -- admin request id
  before      TEXT,                                  -- JSON, NULL for creates
  after       TEXT                                   -- JSON, NULL for deletes
);
-- The trigger is the guard that holds for every role, the table owner included. Defence in depth: the
-- role the gateway runs as should also lose the privileges (it owns nothing, so this is enough):
--   REVOKE UPDATE, DELETE, TRUNCATE ON admin_audit FROM <app role>;
-- Do it once per deployment, after the migrations, by whoever owns the schema; `tests/contract/
-- test_postgres_audit.py` applies exactly this to a dedicated role and checks it.
CREATE FUNCTION admin_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'admin_audit is append-only' USING ERRCODE = 'integrity_constraint_violation';
END;
$$;
CREATE TRIGGER admin_audit_no_change BEFORE UPDATE OR DELETE ON admin_audit
  FOR EACH ROW EXECUTE FUNCTION admin_audit_append_only();
CREATE TRIGGER admin_audit_no_truncate BEFORE TRUNCATE ON admin_audit
  FOR EACH STATEMENT EXECUTE FUNCTION admin_audit_append_only();
