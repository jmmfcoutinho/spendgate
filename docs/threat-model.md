# spendgate threat model and security baseline

Status: proposed, 2026-11-13. Scope: v0.1, local only (SDK, local proxy, Docker Compose). Cloud patterns are on the [roadmap](../ROADMAP.md). Architecture: [architecture.md](./architecture.md). Fail-closed rule: [ADR-0003](adr/0003-fail-closed.md).

Security is tested where the control is built, not at the end. A test id with no test yet is listed as such at the end of section 5, never silently cited.

## 1. Assets

| ID | Asset | Why it matters | Where it lives |
|---|---|---|---|
| A1 | Provider keys | Direct spend on the operator's account | Gateway env or `file:/run/secrets/x` only |
| A2 | Budgets and limits | The product's promise: hard caps | Ledger (`budgets`, `scopes`) |
| A3 | Ledger integrity | Spent and held amounts must be true | Ledger (`reservations`, `calls`, `kill_switches`, `admin_audit`) |
| A4 | Prompts and responses in transit | May contain personal or confidential data | Memory of gateway only; never stored |
| A5 | Admin keys | Can mint keys, raise budgets, read usage | Hashed in ledger `keys`; shown once |
| A6 | Virtual keys | Spend authority inside a budget | Hashed in ledger `keys` |
| A7 | Price catalog | Wrong prices mean wrong charges | `catalog/prices.yaml` (in the image or mounted read-only) |
| A8 | Build and release chain | Compromise reaches every user and A1 | CI, dependencies, image |

## 2. Actors

| Actor | Capability assumed |
|---|---|
| Caller (legitimate app or agent) | Holds one virtual key; may be buggy or looping |
| Malicious caller or stolen key | Valid key, crafts headers and bodies, can send many requests |
| Network attacker on the Compose or host network | Can reach the gateway port, cannot read the host disk |
| Compromised app container | Runs arbitrary code, has a virtual key, no provider key |
| Malicious or compromised provider/upstream | Returns hostile bodies, usage numbers, SSE streams |
| Supply-chain attacker | Poisons a dependency, action or base image |
| Operator (trusted) | Holds admin key and host access; mistakes are in scope, malice is not |

## 3. Trust boundaries

```mermaid
flowchart LR
  subgraph untrusted [Untrusted: callers]
    APP[App or agent container<br/>virtual key only]
  end
  subgraph perimeter [Compose perimeter]
    direction LR
    NI[(network apps: internal, no route out)]
    subgraph gw [Trusted: gateway container, non-root, read-only]
      GWP[spendgate gateway<br/>holds A1 provider keys]
      LED[(ledger file or Postgres)]
    end
    NE[(network egress)]
  end
  OP[Operator<br/>admin key, CLI]
  PROV[Provider APIs<br/>semi-trusted: untrusted responses]
  APP -- "B1: HTTP, key + headers + body" --> NI --> GWP
  OP -- "B2: admin API / CLI" --> GWP
  GWP -- "B3: ledger port" --> LED
  GWP -- "B4: HTTPS, hosts from config" --> NE --> PROV
  CI[CI and registry] -. "B5: supply chain" .-> gw
```

| Boundary | Crosses | Rule |
|---|---|---|
| B1 caller to gateway | Key, headers, body | Nothing trusted; authenticate, size-check, then validate |
| B2 operator to gateway | Admin key | Scoped admin keys, optional separate port, audited |
| B3 gateway to ledger | SQL | Parameterised only; ledger down means refuse |
| B4 gateway to provider | Provider key, prompt | Hosts from config; responses are untrusted input |
| B5 build to runtime | Code, image | Pinned digests and SHAs; scanned; SBOM |

## 4. Threats (STRIDE-lite)

S spoofing, T tampering, R repudiation, I information disclosure, D denial of service, E elevation. Likelihood and impact are before controls (H/M/L). "Residual" is what remains.

| ID | Threat | STRIDE | L/I | Control | Test or note | Residual |
|---|---|---|---|---|---|---|
| TH-01 | Stolen or leaked virtual key used to spend | S, E | H/M | C-01 hashed keys, C-02 per-key scopes and limits, C-03 revoke and rotate without restart | T-AUTH-01, T-AUTH-02, T-AUTH-03, T-AUTH-05, T-KEY-01, T-LIM-01 | Spend up to that key's cap until revoked (bounded by C-02) |
| TH-02 | Admin API abuse (mint keys, raise budgets, read others' usage) | E | M/H | C-04 scoped admin keys, C-05 separate admin port option, C-06 admin audit log | T-ADM-01, T-ADM-02, T-ADM-03 | Operator holding `keys:write` is trusted |
| TH-03 | Bypass: app calls the provider directly | E | H/H | C-07 provider keys only in the gateway, C-08 `apps` network internal, only the gateway on `egress` | T-NET-01, T-NET-02, T-NET-03 (negative test T-NET-04) | App holds its own leaked provider key: see section 6 |
| TH-04 | SSRF: URLs in request bodies (image_url, file URLs, audio) make the gateway or provider fetch internal hosts | T, I | M/H | C-09 upstream hosts from config only; URLs in request bodies are refused (images as `data:` only) unless `fail_open.allow_remote_media: true` (logged at start); gateway never fetches body URLs; no redirect following to new hosts | T-SSRF-01, T-SSRF-02 | With the opt-in, the provider fetches the URL itself (provider-side) |
| TH-05 | Memory exhaustion from large or many bodies | D | H/M | C-10 body-size and concurrency check before reading the body, streaming pass-through, per-key concurrency, container bounds on uploaded audio (EBML integers at most 8 bytes, duration at most 24 h) | T-DOS-01, T-DOS-02, T-DOS-03, T-AUD-01, T-AUD-02, T-AUD-03 | Slow-loris held connections: the key is checked before any byte is read, so unauthenticated callers hold no gate memory (SR-01, T-DOS-01). A keyed caller's body is read under a whole-body deadline and a minimum transfer rate (T-DOS-04, SR-02), is charged to the gate only for the bytes actually buffered, and one key can hold at most a quarter of the gate memory (NF-01). Residual: gate memory is `max(384 MiB, max_body_bytes x 3, max_json_body_bytes x 32)`, so raising `max_json_body_bytes` above about 12 MiB needs a container larger than 512 MiB; one event loop still serves every tenant |
| TH-06 | Ledger tampering (edit DB file, forge rows, SQL injection) | T | L/H | C-11 parameterised SQL only, C-12 ledger file mode 0600 and owned by the gateway user, no ledger mount in app containers, C-13 invariant checks and append-style `calls` | T-LED-01, T-LED-02, T-LED-03, T-MON-01, T-MON-02, T-MON-04 to T-MON-06 | Host root can edit the ledger: accepted (R-02) |
| TH-07 | Supply-chain compromise (dependency, action, base image, scanner) | T | M/H | C-14 pinned digests, SHAs, lockfile with hashes; C-15 pip-audit, image scan, SBOM; C-16 dependency budget (at most 6, 5 used); Dependabot | T-SUP-01 to T-SUP-06 | Zero-day in a pinned dependency |
| TH-08 | Log leakage (bodies, keys, headers, provider keys) | I | M/H | C-17 never log bodies or keys; redaction of `Authorization`, `x-api-key`; labels sanitised | T-LOG-01, T-LOG-02, T-LOG-03 | Operator adds debug logging: documented |
| TH-09 | Runaway agent session inside budget rules (loop burns the whole key budget) | D, E | H/M | C-18 session limits (max cost, calls, duration per `X-Spendgate-Session`), per-key RPM/TPM, kill switch; sessions are per key and a session that hit a limit stays closed | T-SESS-01 to T-SESS-07 | Cost up to the session cap. Residual: a key can mint up to `max_new_sessions_per_hour` sessions an hour, so up to `max_new_sessions_per_hour` x session budget per hour, always inside the parent budgets |
| TH-10 | Header spoofing of attribution (`X-Spendgate-User/Tags/Session`) to dodge or frame another user | S, R | H/M | C-19 attribution is a label, never authority: budgets and limits bind to the key's scope; user/session headers allowed only within the key's scope and charged to the key; values validated (charset, length, count) | T-ATTR-01, T-ATTR-02, T-ATTR-03 | A key holder can mislabel within its own scope: accepted (R-03) |
| TH-11 | Price catalog tampering or stale prices (undercharge) | T | M/H | C-20 catalog schema validation and checksum at load; unpriced means `model_not_priced`; catalog age shown on `/readyz` and metric, warn when older than configured days; read-only mount | T-CAT-01, T-CAT-02, T-CAT-03, T-CAT-05, T-MON-09 | Provider changes price between catalog updates: bounded by C-21 overrun metric |
| TH-12 | Malicious or compromised provider response (huge usage numbers, malformed SSE, endless stream) | T, D | L/M | C-22 usage sanity cap (flags the call with `review_required_at`, emits a metric and logs a warning; the reported usage is always booked, never clamped), SSE parser limits (line, event, total bytes), stream timeout, response size cap | T-PROV-01, T-PROV-02, T-PROV-03, T-ADP-01, T-ADP-02, T-ADP-03, T-ADP-04, T-ADP-05, T-ADP-06 | Honest-but-wrong provider usage is billed as reported; absurd values are flagged for review |
| TH-13 | Clock skew moves period boundaries (budget resets early or late) | T | L/M | C-23 all time from one injected application clock (UTC); stored timestamps come from that clock, not the database; backward jump never shortens a period; `/readyz` warns on skew against the database clock (Postgres `now()`) | T-CLK-01, T-CLK-02, T-CLK-03 | Host clock set maliciously by root: R-02 |
| TH-14 | Many holds without completion exhaust a budget (hold-flooding DoS) | D | M/M | C-24 hold expiry by lease (expired holds charged in full, so slow calls cannot cheat; a client cancel after the request was sent is charged in full unless usage was seen, so cancelling cannot dodge cost), per-key concurrency and RPM cap bound open holds, estimate capped by the model's max output | T-HOLD-01, T-HOLD-02, T-HOLD-03, T-MON-03, T-MON-11, T-MON-10 | A key holder can lock its own budget until expiry |
| TH-15 | Failure of a dependency opens the gate (ledger down, catalog missing, config error) | E | M/H | C-25 fail closed (ADR-0003): `ledger_unavailable`, `model_not_priced`, startup refuses a bad config, a missing FX rate or a missing output ceiling; fail-open only via the logged `fail_open:` config section | T-BLK-01 to T-BLK-06, T-MON-07 to T-MON-09 | None |
| TH-16 | Container escape or privilege abuse after gateway bug | E | L/H | C-26 non-root UID 10001, read-only root, `cap_drop: ALL`, `no-new-privileges`, memory and pids limits | T-CTR-01, T-CTR-02 | Kernel exploit: accepted (R-02) |
| TH-17 | Timing attack on key lookup | I | L/M | C-01 constant-time compare of hashes | T-AUTH-04 | None |
| TH-18 | Egress from the gateway to non-provider hosts (compromised gateway exfiltrates prompts) | I | L/H | Optional egress host allowlist is on the roadmap. v0.1 relies on C-09 and C-26 only | Accepted risk R-01 | See R-01 |
| TH-19 | Unauthenticated probes leak data (`/metrics`, `/readyz`, errors) | I | M/L | C-27 `/livez` minimal; `/readyz` on the data port answers with the status only, its details (reasons, warnings) and `/metrics` are bound to the admin port or an admin key with `usage:read`; labels carry no key secrets or prompt text; error bodies carry code only | T-OBS-01, T-OBS-02, T-OBS-03 | Metrics on the same port when no admin port is set: documented |

Count: 19 threats.

## 5. Controls and proof

Each control maps to at least one test or CI check. Test ids are stable; the file is where the test lives, or is planned to live when the id is listed under "Test ids with no test yet".

| Control | Description | Test id | Planned location |
|---|---|---|---|
| C-01 | Keys stored as prefix + HMAC-SHA256(server pepper, secret); constant-time compare (`hmac.compare_digest`); pepper from `env:`/`file:` | T-AUTH-01 no plaintext secret in DB or logs; T-AUTH-04 compare is constant-time (uses `hmac.compare_digest`, checked by test and ruff `S`) | `tests/contract/test_keys.py` |
| C-02 | Per-key scope, model allowlist, budgets, RPM/TPM/concurrency | T-AUTH-02 key cannot use a model or scope it is not allowed; T-LIM-01 limits enforced per key; T-KEY-01 an empty `allowed_models` is refused, never read as "every model" | `tests/integration/test_auth.py`, `tests/integration/test_limits.py` |
| C-03 | Revoke and rotate without restart; revoked key refused within cache TTL; rotation grace period | T-AUTH-03; T-AUTH-05 a repeated unknown key costs one ledger read per short window, not one per request | `tests/contract/test_keys.py` |
| C-04 | Admin keys with scopes `usage:read`, `keys:write`, `budgets:write`; a virtual key never works on `/admin/v1` | T-ADM-01 each scope allows only its routes; T-ADM-02 virtual key refused on admin | `tests/integration/test_admin.py` |
| C-05 | Admin API optionally on a separate port; Compose does not publish it to apps | T-ADM-03 admin port unreachable from the app network | `tests/perimeter/test_perimeter.py` |
| C-06 | Every admin write recorded in `admin_audit` (actor key prefix, action, time, request id, before/after, no secrets); the table is append-only, enforced by triggers (UPDATE and DELETE abort); no hash chain, so root on the host can still rewrite it (R-02) | T-ADM-04 audit row per write; no secrets in row; T-ADM-05 UPDATE and DELETE on `admin_audit` are rejected | `tests/integration/test_admin.py` |
| C-07 | Provider keys exist only in the gateway container | T-NET-01 no other container has an env var or secret matching any configured provider credential name (all providers); T-NET-04 deliberately leaked key makes T-NET-01 fail | `tests/perimeter/test_perimeter.py` |
| C-08 | `apps` network `internal: true`; only gateway also joins `egress` | T-NET-02 app cannot reach the internet; T-NET-03 app can reach the gateway | `tests/perimeter/test_perimeter.py` |
| C-09 | Upstream hosts only from `spendgate.yaml`; body URLs refused (`data:` only) unless the logged opt-in; never fetched by the gateway; redirects to unconfigured hosts refused | T-SSRF-01 body with an internal or remote URL is refused (`invalid_request`, zero upstream contact) in every nested content position (chat parts, Responses content and `function_call_output.output`, Anthropic image and `tool_result` blocks; `input_file` always), `data:` URLs pass, and the opt-in `fail_open.allow_remote_media` is logged at start; T-SSRF-02 redirect to another host is not followed | `tests/contract/providers/test_ssrf_nested.py` |
| C-10 | Size and concurrency checked before the body is read; streaming of large bodies | T-DOS-01 oversize `Content-Length` gives `request_too_large` with zero bytes read; T-DOS-02 16 parallel 25 MB uploads stay under memory limit; T-DOS-03 chunked body over limit aborted; T-DOS-04 slow client times out; T-AUD-01 a WebM with a several-MiB TimecodeScale is rejected in under 50 ms; T-AUD-02 EBML integers longer than 8 bytes are rejected; T-AUD-03 a measured duration over 24 h is rejected in every container | `tests/integration/test_limits_body.py`, `tests/unit/test_audio.py` |
| C-11 | Parameterised SQL only; no string-built queries | T-LED-01 injection strings in labels, ids, keys leave the schema intact; ruff `S608` in CI | `tests/contract/test_ledger.py` |
| C-12 | Ledger file mode 0600, gateway-owned; not mounted into app containers | T-LED-02 mode check on startup and in Compose test | `tests/perimeter/test_perimeter.py` |
| C-13 | Ledger invariants: `spent + held <= effective_cap + overrun + excess` (effective cap includes active grants); state machine forbids illegal transitions; `calls` append-only except the sweeper-row update on late settle | T-LED-03 stateful property test and multi-process test; T-MON-01 release-then-retry; T-MON-02 fallback within one reservation; T-MON-04 grant add and expiry; T-MON-05 overrun inside cap does not block; T-MON-06 cap lowered then raised | `tests/contract/test_ledger.py`, `tests/contract/ledger_model.py` |
| C-14 | Actions pinned by commit SHA, base image by digest, uv pinned, lockfile with hashes | T-SUP-01 a test rejects `uses:` references that are not a full commit SHA and `FROM` lines or service images without a digest; T-SUP-02 `uv lock --check` runs in the lint job | `tests/unit/test_pins.py`, `.github/workflows/ci.yml` (lint job) |
| C-15 | pip-audit, secret scan, image scan (pinned), SBOM, Dependabot | T-SUP-03 pip-audit; T-SUP-04 image scan; T-SUP-05 detect-secrets | CI jobs |
| C-16 | Base install limited to six dependencies (five used) | T-SUP-06 test counts base dependencies from `pyproject.toml` | `tests/unit/test_dependencies.py` |
| C-17 | Logging allowlist of fields; bodies and key material never logged; `Authorization` and `x-api-key` redacted | T-LOG-01 run scripted calls with a marker prompt and a marker key, grep logs; T-LOG-02 grep DB; T-LOG-03 error bodies contain no key; T-LOG-04 `serve` never prints or creates bootstrap key secrets (`migrate --bootstrap` prints each once) | `tests/integration/test_logging.py` |
| C-18 | Session limits (cost, calls, duration), kill switch per scope | T-SESS-01 `session_limit_reached` at the cap; T-SESS-02 40 parallel loops on one key stop at the cap (SQLite and Postgres): spend never above it, nothing left held, every refusal `budget_exhausted`, no refused call reaches the upstream; T-SESS-03 a kill switch engaged mid-loop refuses the next call (`kill_switch`) and stops a stream still open at the next heartbeat, charged the full hold; T-SESS-04 idle expiry restarts the session; T-SESS-05 `max_new_sessions_per_hour` refuses; T-SESS-06 session header refused (`invalid_request`) on a key without `session_defaults`; T-SESS-07 a session that hit a limit keeps returning `session_limit_reached` for the same id, and two keys on one scope never share a session | `tests/integration/test_sessions.py` (T-SESS-02, T-SESS-03), `tests/integration/test_gateway_refusals.py` (T-SESS-01, T-SESS-06), `tests/contract/test_ledger_sessions.py` (T-SESS-04, T-SESS-05, T-SESS-07) |
| C-19 | Attribution headers validated and subordinate to the key's scope | T-ATTR-01 header cannot move spend to another key's budget; T-ATTR-02 control characters, long values, too many tags rejected; T-ATTR-03 label injection in logs and metrics blocked | `tests/integration/test_attribution.py` |
| C-20 | Catalog validated against `catalog/schema.json` at load; unpriced model refused; age reported | T-CAT-01 invalid catalog stops startup; T-CAT-02 unpriced gives `model_not_priced` with zero upstream calls; T-CAT-03 a catalog older than 30 days (`STALE_CATALOG_DAYS` in `gateway/observability.py`; a constant, not yet a config key) raises a warning on the admin readiness detail, and the age and sha metrics are set at start and refreshed at every scrape; T-CAT-05 every `calls` row records `catalog_sha` and `price_id`; T-MON-09 catalog fail-open knobs (`charge_zero`, `free_statuses` outside 429/529) rejected at load | `tests/unit/test_catalog.py`, `tests/integration/test_admin_readiness.py` |
| C-21 | Overrun tracked and exposed (`spendgate_overrun_total`) | T-CAT-04 metric moves when the provider reports more than the hold | `tests/integration/test_metrics.py` |
| C-22 | Usage sanity flag (never a clamp), SSE parser limits, stream and response size caps | T-PROV-01 absurd usage is booked as reported and flagged (`review_required_at`, metric, warning), never clamped; T-PROV-02 malformed SSE does not crash and is settled at the hold; T-PROV-03 endless stream cut at limit; T-ADP-01 to T-ADP-06 every adapter's `usage`, `error`, `response` and stream collector never raise on hostile bodies or headers (deep nesting, huge numbers, non-ASCII `Retry-After`): unreadable means usage None (the hold is charged) or a mapped error, and counters that do not fit int64 are unreadable; fuzz in 8.2 | `tests/contract/providers/test_hostile_input.py`, `tests/contract/providers/test_retry_after.py` |
| C-23 | One injected application clock; UTC periods; timestamps from the app clock; monotonic for durations; skew warning vs database clock | T-CLK-01 clock jumps back and forward across a period boundary; T-CLK-02 period roll at exact boundary; T-CLK-03 skew between app clock and database clock raises the `/readyz` warning | `tests/unit/test_clock_periods.py` |
| C-24 | Lease expiry; expired holds charged in full; open holds bounded by concurrency | T-HOLD-01 abandoned holds expire and are charged; T-HOLD-02 crash mid-call, another instance recovers; T-HOLD-03 flood of holds from one key is capped by concurrency; T-MON-03 late settle after expiry with charge > hold books the difference as overrun; T-MON-10 owner-versus-sweeper race on one hold (one winner, no double charge); T-MON-11 client cancel after send is charged the full hold unless usage was seen, and free only if never sent; T-LIB-01 library mode keeps a stream longer than one lease alive through the pipeline heartbeat | `tests/contract/test_ledger.py`, `tests/integration/test_limits.py` |
| C-25 | Fail closed on every uncertain state | T-BLK-01 unpriced; T-BLK-02 budget; T-BLK-03 kill switch; T-BLK-04 bad key; T-BLK-05 too large; T-BLK-06 ledger down (no upstream contact). Each gives zero upstream requests; T-MON-07 missing FX and T-MON-08 missing output ceiling are refused at start or request; T-LIB-02 library mode logs every fail-open option at start | `tests/perimeter/test_block.py`, `tests/integration/test_failclosed.py` |
| C-26 | Non-root, read-only, no capabilities, resource limits | T-CTR-01 `docker run <image> id` is UID 10001; T-CTR-02 container started with read-only root and `cap_drop: ALL`, writes outside the data volume fail | `tests/perimeter/test_container.py` |
| C-27 | Minimal probes; sensitive endpoints behind admin scope; no secrets in metric labels | T-OBS-01 unauthenticated `/metrics` refused when an admin port or key is configured; T-OBS-02 labels contain no key secret or prompt text; T-OBS-03 the data-port `/readyz` returns the status code and `{"status"}` only, details need `usage:read` on `/admin/v1/readiness` | `tests/integration/test_admin_readiness.py`, `tests/integration/test_admin_reports.py` |

Count: 27 controls, 92 distinct test ids.

### Test ids with no test yet

`tests/unit/test_threat_model.py` fails when a test id cited in a table above is neither carried by a test (in its name, docstring or a `# T-XX-NN` comment, anywhere under `tests/`) nor listed here. An id listed here is honest about the gap: the control may exist, but no automated test is tagged with it.

- Planned (the control exists or is being built; the test does not carry the id yet): T-ADM-03, T-ATTR-01, T-ATTR-02, T-ATTR-03, T-CAT-04, T-CLK-01, T-CLK-02, T-CTR-01, T-CTR-02, T-DOS-02, T-HOLD-01, T-HOLD-02, T-HOLD-03, T-LOG-02, T-LOG-03, T-MON-01, T-MON-08, T-OBS-02, T-SSRF-02
- CI job (the check is a CI step, not a test): T-SUP-03 (`make audit`, pip-audit), T-SUP-04 (Trivy image scan), T-SUP-05 (`make secrets`, detect-secrets)

## 6. What spendgate does NOT protect against

| Out of scope | Why | Mitigation outside spendgate |
|---|---|---|
| Library mode is cooperative | Code in the same process can skip the SDK and call the provider; the key is in that process | Use proxy mode with the perimeter for anything untrusted |
| A provider key leaked elsewhere (laptop, notebook, another service) | The provider honours the key regardless of spendgate | Rotate at the provider; use provider-side budgets and alerts; give each system its own key |
| Prompt injection and jailbreaks | Content is forwarded; spendgate does not read or judge it | Application-level defences; spendgate only limits the cost |
| Data residency and provider retention | Prompts leave to the chosen provider | Choose regions and contracts per deployment; spendgate does not store prompts |
| Host root or a malicious operator | They can read keys and edit the ledger | OS access control, disk encryption, backups |
| Provider billing differences | Spendgate estimates from the catalog and reported usage; the invoice is the truth | Reconcile monthly against provider invoices (see how-money-is-protected) |
| Network attackers on the host's published port | v0.1 has no TLS termination | Bind to localhost or the Compose network; TLS is a deployment concern |
| Denial of service by volume from the network | Single small process | Put behind a reverse proxy if exposed; out of v0.1 scope |
| Side effects from tool calls in agent runs | Spendgate sees LLM spend, not what the agent does | Agent sandboxing |

## 7. Accepted risks

| ID | Risk | Rationale | Revisit when |
|---|---|---|---|
| R-01 | No egress host allowlist: a compromised gateway could reach any internet host | Gateway code is small and audited; hosts come from config; the Compose network already stops apps; an allowlist needs a forward proxy or nftables, which adds moving parts to a "local first" v0.1 | Hosting beyond one trusted machine, or any `egress` change |
| R-02 | Host root can read keys, edit the ledger, set the clock | Host compromise defeats any single-host tool | A multi-tenant deployment is planned |
| R-03 | A key holder may mislabel user, tags or session within its own scope | Labels are for reporting; money is bound to the key; policy never trusts labels | Per-end-user billing with untrusted callers is needed |
| R-04 | Honest-but-wrong provider usage is billed as reported (C-22 flags absurd values, never clamps) | There is no independent source of truth | Reconciliation against invoices shows drift |
| R-05 | A key holder can lock its own budget with open holds until lease expiry | Bounded by concurrency and lease length; for money it harms only that key's scopes. The process is shared, though: input that costs a lot of CPU or memory can slow every tenant. Found and fixed or bounded in v0.1 review: uploaded audio containers (SR-03, T-AUD-01 to T-AUD-03). Fixed in v0.1: unauthenticated large or stalled bodies (SR-01, SR-02, T-DOS-01, T-DOS-03, T-DOS-04), and one key filling the shared body gate (NF-01); a Matroska file is capped at one million elements (NF-05). One event loop remains a single point of slowdown for any caller (section 6, volume) | Sessions with a very long lease are added, or a multi-tenant deployment is planned |

## 8. Process

- Every new endpoint, provider or config option adds or updates a row here in the same PR.
- A CI check (`tests/unit/test_threat_model.py`) parses this file and fails if a threat row lacks a control id and a test id or an `R-` note, or if a cited test id has no test carrying it and is not listed under "Test ids with no test yet".
- The file is reviewed against the code with the OWASP API Top 10 as a cross-check.
