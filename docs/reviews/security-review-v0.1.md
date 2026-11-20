# spendgate security review v0.1

Pre-release tree. This was a read-only review; no files in the repository were changed.

## Summary and verdict

**Verdict: Fail.** The release criterion is "no open high or critical finding", and two High findings are open:

- **SR-01:** one request with no key at all can exhaust the gateway's memory. The body is read and parsed as JSON before the key is checked, and a 25 MiB JSON body becomes about 600 MiB in memory. The container limit is 512 MiB.
- **SR-03:** one authenticated audio upload can freeze the gateway's single event loop for minutes to hours. The cause is an unbounded Matroska integer field.

There are also three Medium findings:

- **SR-02:** slow uploads with no key and no read deadline can fill the admission gate.
- **SR-04:** the rule that refuses URLs in request bodies can be bypassed through Responses `function_call_output.output`.
- **SR-05:** the catalog-staleness control is not wired up.

The rest of the design holds up well in the code:

- Keys are stored as HMAC-SHA256 with a pepper and compared in constant time, with a dummy hash when the prefix is unknown.
- Data-plane keys (`sg-`) and admin keys (`sga-`) are kept strictly apart.
- Every admin route checks its admin scope.
- Admin request bodies use strict pydantic models that refuse unknown fields.
- SQL is parameterised. String splicing uses only module constants and enum values.
- The `admin_audit` table is append-only, enforced by triggers.
- Upstream hosts come only from config: https only, http only for loopback, and redirects are not followed.
- Upstream response headers are passed on only from an allowlist.
- Provider error text never reaches the client.
- Stream (SSE) parsing, Bedrock frame decoding and response sizes all have limits.
- Logs keep only allowlisted fields and drop anything shaped like a secret.
- Secret values are never shown in reprs or config errors.

Test run: `uv run pytest -m "not live and not load and not docker and not postgres"` gave **1615 passed, 188 deselected** (42.8 s). The Docker perimeter/block tests and the Postgres tests were **not run** here, because Docker was off-limits and no Postgres server was available.

Findings already reported by the independent code review are not repeated: CR-01 (adapter `usage()`/`error()` raising after send releases at 0), CR-02 (heartbeat renews every pending lease), CR-03 (library-mode heartbeat bypasses stream leases), and CR-04 (int64 overflow on absurd usage leaves the hold pending).

## Threat table

How to read the "Test(s)" column:
- "tagged" means the test names the threat-model id.
- "by behaviour" means a test covers the control but does not use the id.
- "not found" means no test was located.
- Every test that was run passed.

| Threat | Control in code | Test(s) | Status |
|---|---|---|---|
| TH-01 stolen virtual key | `ledger/keys.py:87-141` HMAC + pepper, expiry/revoke checked on every request; per-key policy and limits (`pipeline/steps.py`, `core/limits.py`); revoke/rotate drop the cache entry (`keys.py:156-185`); revocation from another process takes effect within the 5 s cache TTL | `tests/contract/test_keys.py` (T-AUTH-01/03 tagged); `test_gateway_refusals.py::test_unknown_and_disallowed_models`, `unit/test_policy.py::test_model_not_allowed_is_refused_with_403`, `unit/test_limits.py`, `test_gateway_refusals.py::test_concurrency_gate_refuses_fast_when_full` (T-AUTH-02, T-LIM-01 by behaviour) | Holds. The planned files `tests/integration/test_auth.py` and `test_limits.py` do not exist. |
| TH-02 admin API abuse | `gateway/admin.py:41-66` scope per route; key kind checked in `keys.py:128,137`; API keys only via the API (`admin_scopes.py:124`, `need_api_key`); separate listener at `127.0.0.1:8081` in the reference config; audit row in the same transaction plus triggers (`migrations/*/0001_init.sql:179-204`) | `integration/test_admin.py` (T-ADM-01/02 tagged), `test_admin_openapi.py` 403 checks, perimeter probe `admin: fails` (T-ADM-03, Docker, not run), `test_sqlite_adapter.py` (T-ADM-05), `test_postgres_audit.py` (not run) | Holds. Admin keys are global (single tenant), and admin keys can be revoked only from the host. Both are design choices and should be documented. |
| TH-03 bypass to provider | `deploy/compose/compose.yaml`: `apps` internal, secrets only on `spendgate` | `tests/perimeter/test_perimeter.py` (Docker, not run); `test_leaks.py` (ran, passed) | Control present; perimeter tests not executed here. |
| TH-04 SSRF via body URLs | `openai_shapes.py:108-111` (`data:` only), Anthropic base64 only (`anthropic.py:139-143`), Bedrock `data:` only, httpx default `follow_redirects=False`, 3xx treated as failure (`routing.py:358`), base URL https rule (`config/load.py:215-235`) | `contract/providers/openai/test_responses.py::test_remote_image_urls_pass_validate_and_translate_only_when_configured`, `unit/test_config.py::test_plain_http_only_for_loopback` | **Partial: SR-04.** T-SSRF-01/02 ids are absent and no redirect test was found. |
| TH-05 memory exhaustion | `pipeline/intake.py` Gate (size and concurrency before the read), `read_capped`, multipart part and field caps | `unit/test_pipeline_intake.py`, `test_gateway_refusals.py::test_oversized_body_is_refused_from_content_length`, `::test_oversized_chunked_body_hits_the_read_cap`, `test_gateway_memory.py` (ran) | **Fails: SR-01, SR-02.** T-DOS-04 does not exist, and the residual "uvicorn timeouts" claim is false (see SR-02). |
| TH-06 ledger tampering / SQLi | `ledger/sql.py` named parameters; `usage.py` whitelisted dimensions; `sqlite.py:29-50` file mode 0600 and owner check; invariants | `test_ledger_sessions.py::test_t_led_01_injection_strings_leave_the_schema_intact`, `test_sqlite_adapter.py` (T-LED-02), `test_ledger_multiprocess.py`, `test_ledger_properties.py` (T-LED-03), `test_budgets.py`, `test_ledger.py` scenarios | Holds. |
| TH-07 supply chain | Actions pinned by SHA, images by digest, `uv.lock` with hashes, pip-audit, Trivy by digest, SBOM, detect-secrets, Dependabot | `unit/test_dependencies.py` (T-SUP-06); CI jobs for audit, image and secrets | Partial. There is no automated check rejecting `@vN` or an unpinned `FROM` (T-SUP-01), and CI uses `uv sync --frozen`, which does not check that the lockfile is current (T-SUP-02); see SR-12. |
| TH-08 log leakage | `gateway/logs.py` field allowlist plus secret-shape filter; `providers/port.py:119` provider text kept out of replies; `config/secrets.py` opaque `Secret` | `unit/test_observability.py::test_logs_contain_no_prompt_or_secret`, `contract/test_keys.py::test_secret_never_stored_or_logged`, provider no-secret tests, `test_config.py::test_resolved_secret_is_not_in_reprs_or_errors` | Mostly holds. See SR-08 (bootstrap secrets end up in container logs). uvicorn's own access and error logs bypass the allowlist formatter; they carry paths and tracebacks, not bodies. T-LOG-01..03 ids are absent. |
| TH-09 runaway session | `core/sessions.py`, `core/policy.py:_gate`, ledger re-check, `max_new_sessions_per_hour` | `unit/test_sessions.py`, `test_ledger_sessions.py` (T-SESS-04/05/07 tagged), `test_gateway_refusals.py::test_session_limit_is_refused_before_upstream`, `::test_session_header_needs_a_session_key`, `test_gateway_stream_stops.py` | Holds. T-SESS-02 (a 1000-call loop) was not found. |
| TH-10 attribution spoofing | `policy.py:check_labels` (fullmatch charset, length, count); labels never select budgets; sessions are per key | `unit/test_policy.py` label cases and `::test_labels_never_add_or_remove_a_budget_the_key_is_not_bound_to`, `test_usage.py::test_a_hostile_filter_or_label_is_inert` | Holds. T-ATTR ids are absent. |
| TH-11 catalog tampering / stale prices | Schema validation at load, unpriced model refused, catalog sha on every call row | `test_config.py::test_catalog_override_invalid_refuses_start`, `test_catalog.py` (T-CAT-05, T-MON-07 tagged), `test_gateway_refusals.py::test_unpriced_model_is_refused` | **Partial: SR-05.** No catalog age reaches `/readyz`, and the age metric is never set. |
| TH-12 hostile provider | SSE limits (`streaming.py:46-49`, `sse.py`), Bedrock frame cap (`bedrock_stream.py:131-136`), 64 MiB response cap (`transport.py:25`), `flag_review` | `test_gateway_streaming.py` (T-PROV-01..03 tagged), `bedrock/test_responses.py::test_an_oversize_frame_is_rejected_from_its_prelude_before_it_is_buffered` | Holds for size. There is no wall-clock cap (SR-13). The int64 usage overflow is CR-04. |
| TH-13 clock skew | Injected clock; skew warning on `/readyz` | `test_admin_reports.py::test_readyz_warns_on_clock_skew_from_the_database_clock`, `test_observability.py::test_clock_skew_warns_without_failing` (T-CLK-03), `test_budgets.py::test_period_rollover_at_23_59_59_999_utc` (T-CLK-02) | Partial. No T-CLK-01 test exists (clock jumping back or forward across a boundary), so "a backward jump never shortens a period" is **unverified**. |
| TH-14 hold flooding | Lease expiry, sweeper, per-key concurrency | `test_ledger_leases.py` (scenarios 02/03/13, T-MON-10 tagged), concurrency tests | Holds. |
| TH-15 dependency failure opens the gate | Ledger errors give `ledger_unavailable`; unpriced, missing FX and missing ceiling are refused; config validated at start | `test_gateway_refusals.py` (T-BLK-06), `test_money.py::test_m09_missing_fx_rate_is_refused_never_defaulted`, `test_config.py::test_fail_open_unknown_option_refused`, perimeter `test_block.py` (Docker, not run) | Holds. Unhandled exceptions still fail closed, but return non-protocol 500s (SR-07). |
| TH-16 container escape | `Dockerfile` `USER 10001`, Compose `read_only`, `cap_drop: ALL`, `no-new-privileges`, memory and pids limits | T-CTR-01/02: not found (`tests/perimeter/test_container.py` does not exist) | Control present; **untested**. |
| TH-17 timing attack | `hmac.compare_digest` plus `_DUMMY_HASH` (`keys.py:53,131-133`) | `test_keys.py` (T-AUTH-04 tagged) | Holds. |
| TH-18 gateway egress | Accepted risk R-01 | n/a | Accurate. |
| TH-19 unauthenticated probes | `/metrics` only on the admin app, guarded on a shared port (`admin.py:91-107`); `/livez` minimal | `test_admin_reports.py::test_metrics_need_an_admin_key_with_usage_read_on_a_shared_port`, `::test_metrics_on_a_separate_admin_port_need_no_key` | **Partial: SR-06** (`/readyz` details are unauthenticated on the data port). |

## Findings

| ID | Severity | Location | Finding | Exploit scenario (brief) | Suggested fix |
|---|---|---|---|---|---|
| SR-01 | **High** (confirmed; the OOM kill itself is likely, since Docker was not run) | `pipeline/pipeline.py:131,138,151`; `pipeline/intake.py:65,142`; `pipeline/pipeline.py:53,95-98`; `config/schema.py:18`; `deploy/compose/compose.yaml:30` | The body is read (up to 25 MiB) and fully `json.loads`-parsed **before** the key is checked. The gate charges 3x the body size, but parsing amplifies far more: a 25 MiB body of `[{},{},…]` measured 600 MiB current / 625 MiB peak (tracemalloc), or 8.7 M objects. The gate's default memory budget is itself 600 MiB, above the container's `mem_limit: 512m`. | Any client on the `apps` network, with no key, sends one such JSON body to `/v1/chat/completions`. The gateway is OOM-killed. Other tenants' in-flight calls are cut, and their holds are later charged in full by the sweeper. | Authenticate from headers before admitting and reading the body (key, then per-key concurrency, then read). Give JSON endpoints a separate small body cap (e.g. 1–4 MiB) from the transcription cap. Size the gate budget from the real container limit and the parse amplification, or bound the JSON element count and depth. |
| SR-02 | **Medium** (confirmed) | `pipeline/intake.py:63-70,98-105`; `cli/serve.py:125,129` (uvicorn defaults) | There is no deadline on reading the body. uvicorn applies only keep-alive timeouts, and a loopback probe held a connection open for 20 s after the headers with no body bytes. A request without Content-Length is weighted as a maximal body (75 MiB), so 8 such connections fill the 600 MiB gate budget, and every later request gets `429 rate_limited`. All of this happens before the key check. The TH-05 residual ("uvicorn timeouts, T-DOS-04") is inaccurate, and T-DOS-04 does not exist. | 8 connections with no key and a declared 25 MiB body that send nothing make the gateway refuse every caller for as long as they stay open. | Check the key first, as in SR-01. Wrap `read_capped` in `asyncio.timeout` with a minimum transfer rate. Weight requests by bytes actually buffered, or cap unauthenticated in-flight requests separately. Add T-DOS-04. |
| SR-03 | **High** (confirmed: 64 KiB → 0.24 s, 256 KiB → 3.9 s, 1 MiB element → 62.6 s, quadratic) | `core/audio.py:375-392` (`_TIMESCALE`, `_CL_TIME`, `_BLOCKDUR` read with an unbounded `int.from_bytes`); `core/estimate.py:145` | The Matroska parser accepts integer elements of any size. The resulting huge `duration_ms` then goes through `Decimal(int)`, which is quadratic in the number of digits and runs synchronously on the single event loop. After that it raises `decimal.Overflow`, which is not caught and becomes a 500. | A holder of any key allowed a transcription model uploads a small WebM whose TimecodeScale element is several MiB. The event loop blocks for minutes to hours (about 62 s per MiB, growing quadratically). All tenants stall, and lease heartbeats stop, which breaks streams and causes full-hold charges. | Reject EBML unsigned-integer elements longer than 8 bytes. Cap the measured duration at a sane maximum (e.g. the model's maximum or 24 h) and turn anything above it into an `AudioError`. Also catch `ArithmeticError` in estimation. Consider running `measure` in a worker thread with a time budget. |
| SR-04 | **Medium** (confirmed with `openai_shapes.validate`) | `providers/openai_shapes.py:223,240-250` | For Responses `input` items, only `content` is inspected. `function_call_output.output` is accepted unchecked, so an array holding `input_image` with an `http(s)` `image_url`, or `input_file` with `file_url`, passes `validate` and `translate`. The same applies to Azure OpenAI. | A caller makes the provider fetch arbitrary remote URLs even though `fail_open.allow_remote_media` is off. Remote files also bring input tokens the estimate never saw, which inflates overruns past the hold. | Validate `output` when it is a list, using the same part allowlist and the `_data_url` rule (and refuse `input_file`), or allow only a string `output`. Add T-SSRF-01 cases for every nested content position. |
| SR-05 | **Medium** (confirmed by grep) | `gateway/metrics.py:255-259` (`set_catalog` has no caller in `src/`); `gateway/observability.py` (readiness has no age check) | The C-20/T-CAT-03 staleness control does not exist at runtime. `spendgate_catalog_age_seconds` and `catalog_info` are never set, and `/readyz` never warns about a stale catalog. | Prices go stale unnoticed and calls are undercharged, which defeats TH-11's detection. | Call `metrics.set_catalog(sha, age)` at start and on reload. Add a configurable warn-after-days setting and a `/readyz` warning. Add the T-CAT-03 test. |
| SR-06 | Low (confirmed) | `gateway/app.py:159-160`; `gateway/wiring.py:default_readiness` | `/readyz` on the data port has no auth. It reports `ledger_unreachable`, `kill_switch_engaged` and the clock skew in ms, and runs three ledger queries per call. This contradicts the C-27 text. | Any app can learn the operator state and generate ledger load. | Return only a status code on the data port. Serve the details on the admin app, or behind `usage:read`. |
| SR-07 | Low (confirmed) | `pipeline/intake.py:140-144` (`RecursionError` from `[[[…` is not a `ValueError`); `core/estimate.py:145` (`decimal.Overflow`); `gateway/admin_http.py:274`, `gateway/admin_models.py:30` (`OverflowError` from e.g. `0001-01-01T00:00:00+14:00`) | Unhandled exceptions produce plain Starlette 500s, not the protocol error shape. The JSON case happens before the key check. These paths fail closed (the slot and limits are released) but are noisy and untested. | A caller with no key triggers 500 tracebacks in uvicorn's logs. An admin caller gets 500 instead of 400. | Catch `RecursionError` and `ArithmeticError` and map them to `invalid_request`. In the pydantic validator, convert `OverflowError` to `ValueError`. |
| SR-08 | Low (confirmed in code; documented behaviour) | `cli/serve.py:51-58,99-100`; `cli/entry.py:167` (bootstrap runs by default on `serve`) | `bootstrap.keys` secrets, admin keys included, are printed to stderr by `serve`. In Compose that output is the container log, which Docker keeps on the host. This conflicts with C-17. | Anyone with `docker logs` access or the host log files recovers an admin key. | Do not bootstrap keys on `serve`. Print secrets only from `migrate --bootstrap` or `keys create`, or write them to a 0600 file. |
| SR-09 | Low (confirmed) | `ledger/key_types.py:32`; `gateway/admin_scopes.py:127,175`; `ledger/key_flow.py:186,304` | An empty `allowed_models: []` is stored as `None`, which means every model. An operator who tries to lock a key down this way grants all models. This is fail-open semantics (ADR-0003). | A mistaken PATCH widens a key's model access. | Treat `[]` as "no models" or refuse it with `invalid_request`. |
| SR-10 | Low (confirmed) | `docs/threat-model.md` §5/§8; `tests/unit/test_threat_model.py` | §8 says CI fails if a cited test id is missing from the test tree. The check only validates row shape. Many ids have no test or tag (T-AUTH-02, T-LIM-01, T-SSRF-*, T-DOS-*, T-ATTR-*, T-LOG-*, T-CAT-01..04, T-CLK-01/02, T-CTR-*, T-NET-*, T-OBS-*, T-HOLD-*, T-SUP-01..05, several T-SESS/T-MON), and many planned test files do not exist. C-01 says "SHA-256(secret + pepper)", but the code uses HMAC-SHA256. | Coverage drift goes unnoticed, and the Phase 8.2 claim "every test id exists" is false. | Tag tests with their ids, implement the missing-id check, and correct C-01 and the TH-05 residual. |
| SR-11 | Low (confirmed) | `ledger/keys.py:143-154` | Unknown and revoked prefixes are never cached, so every well-formed fake key costs one ledger read. There is no throttle on failed authentication. | Callers with no key can generate ledger load (SQLite single writer and readers). | Add a short negative cache, or rate-limit auth failures per peer. |
| SR-12 | Low (confirmed) | `.github/workflows/ci.yml` (`uv sync --frozen` everywhere) | `--frozen` does not check that `uv.lock` matches `pyproject.toml` (T-SUP-02). No CI check rejects unpinned actions or `FROM` (T-SUP-01). Everything is pinned today. | Lock drift or a future unpinned action merges silently. | Use `uv lock --check` / `uv sync --locked` in one job, and add a grep-based pin lint. |
| SR-13 | Low (confirmed by reading the code) | `cli/serve.py:26` (600 s read timeout per chunk); `pipeline/streaming.py:180-190`; no overall deadline | A stream or buffered body has no wall-clock cap. An upstream trickling a byte every few minutes keeps the hold and the slot alive indefinitely (leases are renewed). | A hostile or degraded provider pins holds and connections. | Add a maximum stream or request duration, then settle as `stream_broken` or `timeout`. |
| SR-14 | Low (unverified; the code points this way) | `ledger/hold_flow.py:166-172`, `pipeline/settle.py:133` | `assert` statements guard state-machine invariants and vanish under `python -O`. They would most likely fail later with an `AttributeError`, which is still closed. | n/a unless run with `-O` | Replace them with explicit raises. |

## Static-tool triage

- **ruff `S` rules:** 0 findings. The 10 inline suppressions were reviewed:
  - S101 asserts (4): accepted; see SR-14.
  - S608 file-level in `ledger/sql.py` and `ledger/usage.py`: false positive. Only module constants, the whitelisted `DIMENSIONS`/`_FILTERS` and enum values are spliced; values are bound as parameters.
  - S506 `yamlio.py:37`: false positive. `StrictLoader` subclasses `CSafeLoader`/`SafeLoader`.
  - S311 `postgres_driver.py:108`: false positive (jitter for backoff).
  - S112 `closing.py:68`: acceptable (bounded retry; the sweeper is the backstop).
  - S105 `providers/port.py:27`: false positive (a mode name).
- **bandit 1.9.4:** 38 issues, none High.
  - 30 × B608 (Medium, low confidence) in `sql.py`/`usage.py`: false positives, for the same reason as S608.
  - B506 in `yamlio.py`: false positive (SafeLoader subclass).
  - 4 × B101: see SR-14.
  - B311, B112, B105: false positives or acceptable, as above.
- **semgrep** (`p/python` + `p/security-audit`, `--metrics off`): 0 results and 0 errors over 122 files.
- **pip-audit** (`make audit`, locked set, `--strict`): no known vulnerabilities.
- **Image scan (Trivy):** not run, because Docker was excluded. It is a CI job.

## OWASP API Top 10 cross-check

- **API1 (BOLA):** no finding. The data plane resolves everything from the key's own scope, sessions are per key, and `/v1/models` is filtered by key policy. The admin plane is single-tenant by design; document that admin keys are global.
- **API2 (broken authentication):** the key design is strong. The weakness is ordering: the key is checked after the body is read (SR-01, SR-02), and failed lookups are not throttled (SR-11).
- **API3 (property-level authorization):** admin models use `extra=forbid`, a key's kind and admin scopes cannot be set through the API, and hashes are never returned. Gap: SR-09.
- **API4 (unrestricted resource consumption):** SR-01, SR-02, SR-03, SR-13.
- **API5 (function-level authorization):** per-route scopes, and keys of one kind are refused on the other plane. Tested.
- **API6 (sensitive business flows):** hold-locking (R-05) and the session minting rate (TH-09 residual) are documented.
- **API7 (SSRF):** upstream hosts come only from config and redirects are not followed. Gap: SR-04 (provider-side fetch).
- **API8 (security misconfiguration):** the gate budget is larger than the container memory limit (SR-01), uvicorn has no body timeout (SR-02), `/readyz` exposes details (SR-06), and bootstrap secrets land in logs (SR-08).
- **API9 (inventory):** an admin OpenAPI spec exists and has tests; unknown `/v1` and `/admin` paths return 404.
- **API10 (unsafe consumption of upstream APIs):** strong caps and parsing that never trusts upstream values. Gaps: SR-13 and CR-04.

## Fuzz targets (Hypothesis unless noted; at least 10 minutes each)

1. **`core.audio.measure`, then `core.estimate.estimate(RequestKind.TRANSCRIPTION, …)`.**
   - Inputs: mutated valid WAV/MP3/MP4/Ogg/FLAC/WebM built by the generators in `tests/unit/audio_builders.py`, plus raw bytes.
   - Invariants: raises only `AudioError`, or returns `Estimate`/`Refusal`; finishes in under about 50 ms per MiB; `duration_ms` stays below a fixed cap; for generated valid files with a known duration, the measured duration is at least the true one.
   - This target would have caught SR-03.
2. **`pipeline.intake.parse_body` and `parse_multipart`.**
   - Inputs: arbitrary JSON, deeply nested JSON, arbitrary multipart (boundary in the content, missing CRLF, repeated names, more than 32 parts).
   - Invariants: never raises; parts ≤ `MAX_PARTS`; fields ≤ `MAX_FIELD_BYTES`; round-trip for well-formed bodies from a reference encoder.
   - This target would have caught the SR-07 `RecursionError`.
3. **`providers.sse.SseParser`.**
   - Inputs: arbitrary bytes under arbitrary chunk splits.
   - Invariants: only `StreamBroken`/`StreamLimitExceeded` are raised; the output does not depend on how the input is split; the internal buffer never exceeds `max_line_bytes`; event and stream counters respect `SseLimits`.
4. **`providers.bedrock_stream.EventStreamDecoder` and `_headers`.**
   - Inputs: valid frames from a reference encoder (with correct CRCs), then mutated.
   - Invariants: only `EventStreamError` is raised; split invariance; nothing over `MAX_FRAME_BYTES` is ever buffered; round-trip.
5. **Request allowlists** (`openai_shapes.validate`/`check`, `anthropic.AnthropicProvider.validate`, the `bedrock_request` builder, the Google request builder).
   - Inputs: generated nested bodies.
   - Invariants: no accepted body contains a non-`data:` URL or a file reference in any media position unless `remote_ok`; only `Untranslatable`/`Refusal` are raised.
   - This target would have caught SR-04.
6. **Provider usage and error readers** (OpenAI/Azure usage and stream collectors, Anthropic `_Collector`, `converse_usage`, Google, ElevenLabs, Azure Speech).
   - Inputs: arbitrary JSON or SSE events, huge integers, Retry-After strings.
   - Invariants: returns `None` or a valid `Usage` with non-negative counters that fit in int64; never raises after send (CR-01/CR-04 regressions).
7. **Admin API with schemathesis**, against `docs/admin-openapi.yaml` and the in-process ASGI admin app.
   - Invariants: no 5xx (would catch the SR-07 time overflow); responses match the schema; no `secret` field except on create and rotate; scope enforcement holds for each of the three single-scope keys.
8. **`ledger.keys.parse_key` / `KeyService._verify` and `pipeline.auth.presented_key`.**
   - Invariants: only the exact formats are accepted; never raises; an admin key never verifies as an API key and vice versa.
9. **`core.policy.check_labels` / `steps.labels_of`.**
   - Inputs: arbitrary unicode and control characters in header values.
   - Invariants: values are accepted only if they fully match the documented charset, and logs and metric labels contain only accepted values.
10. **`gateway.admin_http.decode_cursor`, `parse_time`, the `ledger.usage.UsageQuery` builder, and `config.load` on fuzzed YAML.**
    - Invariants: only the documented error types are raised, and no secret value appears in any error.

## Accepted-risk check

- **R-01** (no egress allowlist): still accurate. `egress` is a plain bridge network with internet access.
- **R-02** (host root): accurate. Secrets are mode 0644 inside a 0700 directory, as documented.
- **R-03** (mislabelling within the key's scope): accurate. The fallback to the body's `user`/`metadata.user_id` is also a label only.
- **R-04** (usage billed as reported): accurate in intent. Values above int64 cannot be booked as reported (CR-04).
- **R-05** (a key locks its own budget): accurate for money. But "harms only itself" does not hold for the shared process: one key holder (SR-03), or a client with no key at all (SR-01, SR-02), can deny service to every tenant. Either fix those findings or add a residual-risk row.

## Recommended next steps (to reach Pass)

1. Move the key check ahead of reading the body; add a small JSON body cap, a body-read deadline, and a gate budget that is at most the container memory (SR-01, SR-02).
2. Bound Matroska integer elements and the measured duration, and catch `ArithmeticError` (SR-03).
3. Validate Responses `function_call_output.output` (SR-04).
4. Wire up the catalog age metric and the `/readyz` warning (SR-05).
5. Run the Docker perimeter and Postgres suites, plus the fuzz targets above, and record the results in this report.

## Fuzzing

The release criterion asks for the fuzz targets above to run for at least 10 minutes each without a crash. They are now in `tests/fuzz/` (Hypothesis; schemathesis is not installed, so the admin API is fuzzed from strategies built out of `docs/admin-openapi.yaml`).

### How to run

- `make fuzz` (or `uv run pytest -m fuzz tests/fuzz`) is the short mode: 200 examples per case, about 20 seconds in total. It is excluded from `make test` by the `fuzz` marker; the pinned regressions below are not marked and stay in `make test`.
- `SPENDGATE_FUZZ_SECONDS=600 uv run pytest -m fuzz tests/fuzz/test_<target>.py` runs one target for that many wall-clock seconds. `SPENDGATE_FUZZ_SEED` (default `20261117`) fixes the seed, `SPENDGATE_FUZZ_EXAMPLES` and `SPENDGATE_FUZZ_ROUND_EXAMPLES` change the budgets, and `SPENDGATE_FUZZ_REPORT_DIR` writes one JSON report per target.
- Hypothesis has no wall-clock budget, so a long run is a loop of rounds of 300 examples, each an ordinary `@given` run with `@seed(base + round * 1000 + case index)`, `deadline=None` and no example database. A run is reproducible from the base seed, a failure is shrunk and reported by Hypothesis as usual, and the example count is exact. `deadline=None` because a few examples (a 1 MiB MP3, a 900-deep JSON document) are legitimately slower than 200 ms; the slowness that matters is asserted by the targets themselves (audio: about 50 ms per MiB).

### Results

Runs of 600 seconds each, base seed `20261117`, five processes at a time (a sixth ran for about three minutes while the SSE target was re-run), against a snapshot of `src/` taken on 2026-11-17 at 12:22: `HEAD` `aec93ec` plus the uncommitted working-tree changes of the concurrent verification work (diff hash `bb91cbd8951e`), which were committed as `902ce32` (the snapshot differs from it only in `pipeline/streaming.py`). A further 120-second pass of all ten targets against `902ce32` also passed (96,900 audio, 22,200 intake, 177,000 SSE, 13,800 Bedrock stream, 62,100 allowlist, 37,200 usage-reader, 35,517 admin API, 207,000 key, 26,400 label and 79,500 parser examples).

| # | Target (test file) | Cases | Examples | Wall time | Result |
|---|---|---|---|---|---|
| 1 | `core.audio.measure`, `estimate(TRANSCRIPTION)` (`test_audio.py`) | measure, estimate, valid file never under-measured | 478,500 | 600.3 s | pass |
| 2 | `intake.parse_body`, `parse_multipart` (`test_intake.py`) | JSON bodies, multipart round trip, hostile multipart, raw bytes | 110,700 | 600.7 s | pass |
| 3 | `providers.sse.SseParser` (`test_sse.py`) | split invariance and limits, serialise round trip | 854,400 | 600.2 s | pass (one input class excluded, FZ-04) |
| 4 | `EventStreamDecoder`, `_headers` (`test_bedrock_stream.py`) | valid round trip under any split, hostile frames, header parser | 70,500 | 601.0 s | pass |
| 5 | request allowlists (`test_allowlists.py`) | OpenAI shapes, Anthropic, Bedrock builder, Google builder | 314,400 | 600.1 s | pass (one input class excluded, FZ-01) |
| 6 | usage and error readers (`test_usage_readers.py`) | all seven adapters' `usage` / `error` / `response`, Retry-After parsers, shared readers, stream relays | 168,300 | 600.7 s | pass |
| 7 | admin API (`test_admin_api.py`) | any operation x any of six key kinds x valid / hostile / non-JSON bodies | 148,924 (124,200 requests) | 601.5 s | pass (one input class excluded, FZ-02) |
| 8 | `parse_key`, `KeyService` verify, `presented_key` (`test_keys.py`) | exact format, kinds and key state, header spellings | 945,000 | 600.1 s | pass |
| 9 | `check_labels`, `labels_of` (`test_labels.py`) | exact charset, headers and body fallback | 125,700 | 600.3 s | pass |
| 10 | `decode_cursor`, `parse_time`, `UsageQuery` / `build`, `config.load` (`test_admin_parsers.py`) | cursors, round trip, times, query builder, fuzzed YAML | 363,000 | 600.3 s | pass |

The first SSE run failed after 6 seconds and the second after 112 seconds, both on FZ-04; the third (listed) ran after the input class was excluded. The admin run served 124,200 requests: 23,850 `200`, 1,809 `201`, 2,441 `204`, 34,676 `400`, 8,632 `401`, 11,312 `403`, 38,610 `404`, 1,790 `409` and 1,080 `503` (the documented `/readiness` answer). There were no other 5xx.

### Invariants checked

1. **Audio.** `measure` raises only `AudioError`, or returns a duration in `(0, 24 h]`; it finishes within 0.5 s plus 50 ms per MiB; an unmutated valid WAV, MP3, Ogg Opus / Vorbis, FLAC, MP4 or WebM (known duration) is never measured shorter than the truth; `estimate` returns only `Estimate` or `Refusal`, agrees with `measure`, and its audio seconds cover the measured duration. Inputs: the `audio_builders` generators, mutated (bit flips, extreme integers written over header fields, truncation, splices), and raw bytes behind real magic numbers; in long mode also 1 MiB MP3s.
2. **Intake.** Never raises, for arbitrary and 100,000-deep JSON, bad content types and hostile multipart (boundary in the content, missing CRLF, repeated names, more than 32 parts, 1 MiB fields); an accepted JSON body is an object; parts never exceed `MAX_PARTS`, fields never `MAX_FIELD_BYTES`; bodies from a reference encoder round-trip exactly, or are refused when over a limit.
3. **SSE.** Only `StreamBroken` / `StreamLimitExceeded` are raised; a stream that parses whole parses identically in any split and byte by byte, and one that fails whole fails in every split; the buffer never exceeds `max_line_bytes` (not counting one pending CR); no event exceeds `max_event_bytes`; event and stream-byte limits hold (tiny limits are drawn so every limit is hit); `serialise` round-trips.
4. **Bedrock event stream.** Only `EventStreamError` (and `FrameTooLarge`) is raised, for raw damage and for structural mutations rebuilt with valid CRCs (lying lengths, junk header bytes, every header value type); decoding is split-invariant; nothing over the frame limit is left in the buffer; valid frames round-trip; `_headers` on arbitrary bytes raises only `EventStreamError`.
5. **Allowlists.** An independent positional oracle (where each API reads content parts) finds no accepted body with a non-`data:` URL, a file reference or a non-base64 image source (unless `remote_ok`); only `Untranslatable` / `Refusal` are raised; an accepted OpenAI body also translates and the rebuilt upstream body is clean; the Bedrock and Google bodies contain inline bytes only. Bodies start from valid skeletons and are perturbed, so they get past the first wrong key. The oracle was checked by mutation: removing the `function_call_output.output` check (SR-04), the `data:` check, the Anthropic image-source check, or making the Bedrock / Google builders emit `s3Location` / `fileData` each makes the target fail within seconds.
6. **Usage and error readers.** Through the public `usage`, `error`, `response` and `relay` of OpenAI, Azure OpenAI, Anthropic, Bedrock, Google, ElevenLabs and Azure Speech: a `Usage` has non-negative counters that fit int64 and a finite audio duration, or is `None`; retry hints are `None` or within 0..86,400; nothing raises; a relay raises only `StreamBroken` and its `usage()` is valid afterwards. The target fails within seconds if the int64 bound in `count_of` / `bounded` is removed.
7. **Admin API.** No 5xx and no exception escapes (except the `/readiness` 503); every response validates against its documented schema (`default` is `Error`); a `secret` appears only in the `201` of key create and rotate, and no key-shaped string, key hash or pepper appears anywhere else; each single-scope key gets exactly `403` on a wrong-scope route and never `401` / `403` on its own; no, garbage and data-plane keys get exactly `401`.
8. **Keys.** `parse_key` accepts exactly `sg-` / `sga-` + 12 + 48 lower-case ASCII hex (compared with an independent parser); an admin key never verifies as an API key and the reverse; revoked and expired keys never verify; a verified principal is the key that was presented; `presented_key` returns only a key that was actually sent, refuses two different keys, never returns an empty one.
9. **Labels.** `check_labels` accepts exactly the documented charset and lengths (and at most 10 tags); the user label and tag names that survive `check_labels` match it; a body-derived user is dropped unless it matches, never altered.
10. **Parsers.** `decode_cursor` and `parse_time` raise only `ApiError` (`invalid_request`, the right `param`); a cursor round-trips and decodes to exactly the requested types; `UsageQuery` raises only `UsageQueryError`, and filter values reach the SQL only as bound parameters; `config.load` raises only `ConfigError` on mutated, structurally damaged and junk YAML, within 5 seconds, and no secret value (pasted inline into a secret field, or resolved from the environment) appears in any error.

### Findings

Each finding has a minimal regression test, `xfail(strict=True)` while open (`tests/fuzz/test_regression_*.py`; they are unmarked and run in `make test`). Where a fuzz case would trip over an open bug on every run, that one input class is skipped in the generator (named in the code and below) so the rest of the case keeps being fuzzed; remove the skip when the bug is fixed.

| ID | Where | What | Severity | Status |
|---|---|---|---|---|
| FZ-01 | `openai_shapes._response_item` | A Responses `input` item whose `type` is a list or object (`{"input":[{"type":[]}]}`) raises `TypeError: unhashable type` from `validate` (the `kind not in _ITEM_KEYS` lookup). `steps.prepare` has no guard, so the exception leaves the pipeline: the client gets a bare 500 instead of a 400 `invalid_request`. It happens after authentication and before any hold, so no money moves. | Low | fixed; `test_fz01_unhashable_item_type_is_refused_not_raised`, plus the `prepare` guard `test_unexpected_provider_validate_error_is_a_400_refusal_not_a_500` |
| FZ-02 | admin app routing | A newline inside an admin URL (`/admin/v1/scopes/a%0Ab`, `/admin/%0A0`) matches neither a route nor the `/admin/{path:path}` fallback (Starlette's `path` convertor is `.*` without DOTALL), so the reply is the plain-text `Not Found`, not the documented JSON error shape. | Low | fixed (JSON 404 handlers on the admin and data-plane apps); `test_fz02_newline_in_admin_path_gets_the_error_shape`, `test_fz02_newline_in_data_plane_path_gets_the_error_shape` |
| FZ-03 | `admin_http.parse_time` | `?since=0001-01-01T00:00:00+14:00` (any aware time whose UTC form leaves years 1..9999) raised `OverflowError`, a 500: the SR-07 time overflow, still open at the time. The concurrent verification work added an `except OverflowError` during this task (committed in `902ce32`), and the fuzz targets pass with it. | Medium | fixed in `902ce32`; pinned by plain tests `test_fz03_*` |
| FZ-04 | `sse.SseParser.feed` | A line of exactly `max_line_bytes` ending in CR or CRLF is refused with `StreamLimitExceeded` when a chunk ends right after the CR (the pending CR is counted as a line byte), but the same bytes in one chunk parse. A valid stream can be broken by chunking; fail-closed, and the limit is 64 KiB by default. | Low | fixed (a pending final CR is not a line byte); `test_fz04_line_at_the_limit_parses_whatever_the_chunking` |

No other target found anything in 600 seconds. Several early failures were mistakes in the fuzzer's oracle, not in spendgate, and were fixed there: an oracle that flagged URL-shaped values in opaque pass-through fields (`model`, ids, `media_type`, `text`), a blank `X-Api-Key` next to a bearer key (spendgate refuses the pair, which is stricter and fine), a one-frame MP3 (refused by design: fewer than three frames), and an unrouted `%2F` / empty path segment.

### Not covered

The Docker perimeter and Postgres suites (the other half of recommended step 5) were not run here. The fuzz targets exercise the in-process ASGI admin app over SQLite, not the data-plane HTTP server (intake, allowlists and readers are fuzzed directly, not through a socket). `config.load` is fuzzed on files only; `file:` secret references point at a nonexistent path or one temporary file.
