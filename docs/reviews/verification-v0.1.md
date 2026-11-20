# spendgate v0.1 verification of review fixes (pre-release tree)

Scope: the fixes for CR-01…CR-13 and SR-01…SR-14 checked against `docs/reviews/code-review-v0.1.md`, `docs/reviews/security-review-v0.1.md` and ADR-0009. This was a read-only review and no repository file was changed. `tests/fuzz/` was left out: gates were run with `--ignore tests/fuzz` / `--exclude tests/fuzz`. Four behaviours were reproduced with throwaway scripts outside the repository that drive `tests/integration/harness.py` (marked "probe" below).

## Verdict

**Ready for a v0.1 tag, on one condition.** The bar from the security review ("no open high or critical finding") is met:

- All five High findings (CR-01…CR-04, SR-01) and the code-review High CR-03 are closed, and each has a passing test named for it.
- The money rules hold on every path exercised:
  - one hold per request, before anything is sent;
  - exactly one release or settle;
  - an adapter that raises after the send is charged;
  - a hold the owner failed to close reaches the sweeper.

**The condition:** the fixes introduced one new **Medium** (NF-01). One authenticated key can fill the process-wide body gate with three chunked uploads, and every other tenant then gets 429. Either fix it (small change) or record it as an accepted risk before tagging.

The other new findings are Low:

- NF-02: the SR-13 deadline is not enforced when the client stops reading a stream.
- NF-03: a lost hold commit now charges a call that was never sent.
- NF-04: the stream close path has no "owned" cleanup if it raises.
- NF-05: audio parsing still blocks the event loop for seconds.
- NF-06: documentation drift.

Open partials: SR-07 (admin time overflow still gives 500), SR-10, SR-11, CR-10 and SR-13. The Postgres and Docker suites were not run. That includes the new per-id `RES_RENEW` on Postgres.

## Finding status

| Id | Status | Fix location | Proving test(s) (all run, all pass) | Notes |
|---|---|---|---|---|
| CR-01 | **Closed** | `pipeline/routing.py:263-267` (flight kept until `_record`), `:334-344` `_unreadable`, `:350-355`, `:383-386`; `providers/port.py:297-333` `never_raises_usage/response/error` on all 7 adapters; `pipeline/streaming.py:291-293` | `test_review_fixes.py::test_cr_01_usage_that_raises_after_send_is_charged_the_full_hold`, `::test_cr_01_error_that_raises_after_send_is_charged_the_full_hold`, `::test_cr_01_streamed_usage_that_raises_is_charged_the_full_hold`; `contract/providers/test_hostile_input.py::test_t_adp_01…06`; `test_retry_after.py` | Two layers: adapters never raise, and the pipeline catches any exception after the send as "unreadable" (full hold). `routing.py:365-367` (`response()` raising after a recorded OK) is uncovered by tests. By reading it charges the actual usage, which is correct. |
| CR-02 | **Closed** | `ledger/sql.py:103` (`RES_RENEW … WHERE id = :id AND owner_instance = :owner`); `ledger/settle_flow.py:263`; `ledger/engine.py:67-72`; `pipeline/upkeep.py:29-45`; `pipeline/closing.py:25-27,60-62,68-93` (backoff 0.05/0.25 s, unsettled retry, give-up after 10 leases); `pipeline/pipeline.py:115,201,219,290-297` | `test_review_fixes.py::test_cr_02_heartbeat_after_a_failed_settle_lets_the_sweeper_charge_the_full_hold`, `::test_cr_02_an_unsettled_close_is_retried_by_the_heartbeat`; `contract/test_ledger_leases.py::test_cr_02_renew_all_leaves_a_hold_the_caller_did_not_name_to_the_sweeper[sqlite]` | Heartbeat renews only `(owned ∪ open streams) − unsettled`. The Postgres variant was skipped. See NF-03 and NF-04 for edges of the new "owned" model. |
| CR-03 | **Closed** | `local/runtime.py:108-125` (`_maintain` calls `pipeline.renew_leases()` + `watch_streams()`, then sweeps) | `integration/test_local_stream_leases.py::test_t_lib_01_a_library_stream_longer_than_one_lease_is_not_cut` (3 leases long) | |
| CR-04 | **Closed** | `pipeline/settle.py:117-141` (`_fits`, `read_usage` → `absurd`, full hold, flagged); `providers/port.py:43-61` (`count_of`, `bounded`) | `test_review_fixes.py::test_cr_04_usage_that_does_not_fit_the_ledger_charges_the_hold_and_flags_review`; `unit/test_pipeline_settle.py::test_cr_04_…`; `test_hostile_input.py::test_t_adp_02/03` | The stream path also goes through `read_usage` (`streaming.py:294`). |
| CR-05 | **Closed** | `pipeline/answers.py:15-30` (`charged = … if closed.booked else None`) | `test_review_fixes.py::test_cr_05_no_cost_header_when_the_ledger_did_not_book` | The test covers the 2xx path. The error path uses the same `charged` value. |
| CR-06 | **Closed** | `pipeline/routing.py:53,126-127,220-221,226-228,389-397` (`Cause.QUOTA`) | `test_review_fixes.py::test_cr_06_quota_on_every_attempt_is_upstream_unavailable_without_retry_hints` | The free failover is kept. Second call while cooling: 502, no retry hint. |
| CR-07 | **Closed** | `gateway/wiring.py:90-122` (`catalog_age_seconds`, `bind_catalog_metrics`); `cli/serve.py:118`; `gateway/metrics.py:218,249-258` (`before_render`) | `integration/test_admin_readiness.py::test_t_cat_03_a_stale_catalog_warns_on_readiness_and_ages_the_metric` | Nit: an unknown age is published as `0` (fresh), `wiring.py:118`. In practice this cannot happen, because every entry has `effective_from`. |
| CR-08 | **Closed** | `providers/port.py:68-99` (one `retry_after_seconds` + `retry_after_ms_seconds`, used by all adapters); `pipeline/settle.py:173-189` (`attempt_record`, single builder; `Reading` priced once); `logs.key_prefix` and `Metrics.observe_call` removed | `contract/providers/test_retry_after.py` (24 cases incl. `²`, 5000 digits, `1e999`, NaN); `test_hostile_input.py::test_t_adp_04` | Out of scope: `client/errors.py:182` (SDK parsing the gateway's own header) still uses `isdigit()`+`int()`. The input is trusted, but it is the same pattern. |
| CR-09 | **Closed** | `local/runtime.py:99-102` (WARNING when `allow_remote_media` is on) | `test_local_stream_leases.py::test_t_lib_02_library_mode_logs_fail_open_options_at_start` | |
| CR-10 | **Partly closed (as known)** | `contract/test_ledger_leases.py:126-150` | `::test_t_mon_10_owner_and_sweeper_race_has_one_money_transition[sqlite]`; ordered: `::test_scenario_12_t_mon_10_owner_first…`, `::test_scenario_12_t_mon_10_sweeper_first…` | The race test now asserts the invariant over whatever mix happened (`spent = settled·0.40 + rest·1`, held 0). It still cannot force both orders; the two ordered tests cover them. Nit: the explanatory string sits after the first statement, so it is not a docstring. |
| CR-11 | **Closed** | `core/estimate.py:335-337,380` (`ROUND_CEILING` on `Decimal`) | `unit/test_estimate.py::test_cr_11_audio_token_ceiling_rounds_up_a_fraction_of_a_token` | |
| CR-12 | **Closed** (docs option) | `docs/architecture.md:43` documents `pipeline/adapters.py` as the composition helper | none (docs); `lint-imports` 6 contracts kept | No forbidden contract was added. The review allowed either option. |
| CR-13 | **Closed** | `gateway/metrics.py` (table-built) | `scripts/lightweight_report.py`: 0 functions over 50 lines | |
| SR-01 | **Closed** (see NF-01) | `pipeline/pipeline.py:126-178` (key → Content-Length → per-key intake ticket → gate → read); `pipeline/steps.py:285-297` (`admit_intake`); `pipeline/intake.py:20-26` (`JSON_AMPLIFICATION=32`, `DEFAULT_MEMORY_BYTES=384 MiB`); `config/schema.py` `max_json_body_bytes` 4 MiB | `test_review_fixes.py::test_sr_01_an_unauthenticated_25_mib_json_body_is_refused_unread`, `::test_sr_01_json_has_its_own_small_cap_checked_before_the_read`, `::test_sr_01_default_gate_memory_stays_under_the_reference_container_limit`, `::test_sr_01_parallel_amplifying_json_bodies_stay_under_the_gate_budget` (slow; ran), `::test_sr_01_per_key_uploads_are_limited_before_the_gate_and_freed_after_the_read` | A caller with no key never reads a byte or takes a gate place. Gate memory is `max(384 MiB, max_body·3, max_json·32)` (`pipeline.py:95-100`), so raising `max_json_body_bytes` above 12 MiB pushes it past the 512 MiB container limit. This is not documented. |
| SR-02 | **Closed for unauthenticated callers** (residual → NF-01) | `pipeline/intake.py:122-150` (`asyncio.timeout` + 1 KiB per 5 s progress rule → 408); `pipeline.py:163` | `test_review_fixes.py::test_sr_02_a_stalled_body_is_refused_with_408_and_frees_its_place`, `::test_sr_02_an_unauthenticated_stalled_body_never_holds_a_place` | Bounded at 30 s by default. Authenticated stalls can still monopolise the gate (NF-01). |
| SR-03 | **Closed** (residual → NF-05) | `core/audio.py:31-39` (24 h cap), `:354-361` `_uint` ≤ 8 bytes, used at `:388-405`; `:57` catches `ArithmeticError`; `core/estimate.py:148` | `unit/test_audio.py::test_t_aud_01_webm_with_a_huge_timecode_scale_is_rejected_fast`, `::test_t_aud_02_…[3 ids]`, `::test_t_aud_03_a_duration_beyond_24_hours_is_rejected_in_every_container` | The quadratic blow-up is gone. Parsing is still synchronous and linear (NF-05). |
| SR-04 | **Closed** | `providers/openai_shapes.py:240-258` (`content` and `output` both go through `_response_parts`; a non-string, non-list `output` is refused; `input_file` is not in the allowlist) | `contract/providers/test_ssrf_nested.py::test_t_ssrf_01_*` (OpenAI and Azure, Responses and Chat; Anthropic nested) | |
| SR-05 | **Closed** | `gateway/wiring.py:90-149`; `gateway/observability.py:24,58,97`; `cli/serve.py:118` | `test_admin_readiness.py::test_t_cat_03_…`; `unit/test_observability.py` | The warn threshold is a constant (30 days, `observability.py:24`), not the configurable setting the review suggested. The code comment defers it. |
| SR-06 | **Closed** | `gateway/observability.py` `ReadyStatus.body(detail=False)`; `gateway/admin.py:47`; `gateway/admin_reports.py:135-142` | `test_admin_readiness.py::test_t_obs_03_data_port_readyz_names_no_reason_warning_or_detail`, `::test_t_obs_03_readiness_detail_needs_usage_read` | |
| SR-07 | **Partly closed** | Fixed: `pipeline/intake.py:195-196` (`RecursionError`), `pipeline/steps.py:164`, `core/estimate.py:148`. **Not fixed:** `gateway/admin_models.py:20-30` (`rfc3339_ms`) and `gateway/admin_http.py:264-276` (`parse_time`) | `test_review_fixes.py::test_sr_07_deeply_nested_json_is_invalid_request_not_a_500`, `::test_sr_07_arithmetic_error_in_estimation_is_invalid_request`; no admin test | Verified: `rfc3339_ms('0001-01-01T00:00:00+14:00')` and `parse_time(…)` raise `OverflowError`. Pydantic lets it through `KeyCreateIn`/`KeyUpdateIn`, and `refusal_for` returns None, so the admin caller gets a 500 `internal_error` instead of 400. `GrantCreateIn.valid_from/valid_until` use the same `Time` type. Fix: catch `OverflowError` in both functions and raise `ValueError`/`ApiError`. |
| SR-08 | **Closed** | `cli/serve.py:101-109` (`create_keys=False` and a stderr notice); `gateway/bootstrap.py` `create_keys` | `test_admin_bootstrap.py::test_t_log_04_serve_bootstraps_scopes_and_budgets_but_never_prints_or_creates_keys`, `::test_t_log_04_migrate_bootstrap_still_creates_and_prints_each_key_once` | |
| SR-09 | **Closed** | `ledger/key_flow.py:281` `_models_not_empty` (create and set_policy); `gateway/admin_models.py:38` `ModelList(min_length=1)`; `gateway/admin_scopes.py:127,175`; `gateway/bootstrap.py` | `contract/test_keys.py::test_set_policy_empty_models_is_refused_none_means_all_and_scope_cannot_move[sqlite]`; `test_admin.py:355,362` (400); `test_admin_bootstrap.py::test_t_key_01_…` | Stale comment at `ledger/key_types.py:32` ("empty is stored as None (= every model)"). `model_allowed` treats `()` as "no models", so library mode fails closed. |
| SR-10 | **Partly closed (as known)** | `docs/threat-model.md:133` ("Planned" list of 22 ids), §8; C-01 corrected to HMAC-SHA256 (`:99`); `tests/unit/test_threat_model.py:77-83` | `test_threat_model.py::test_every_cited_test_id_has_a_test_or_is_listed_as_not_yet_written`, `::test_a_cited_id_with_no_test_and_no_listing_is_caught` | Still left: 22 ids have no test. Control rows cite files that do not exist (`tests/integration/test_auth.py`, `test_limits.py`, `test_limits_body.py`, `test_logging.py`, `test_failclosed.py`, `tests/perimeter/test_container.py`). The TH-05 residual and R-05 now describe the pre-fix state (NF-06). |
| SR-11 | **Partly closed (as known)** | `ledger/keys.py:150-166` (miss cache `min(ttl, 1 s)`, bounded, cleared on create/rotate) | `contract/test_keys.py::test_t_auth_05_a_repeated_unknown_key_costs_one_ledger_read_per_window[sqlite]`, `::test_t_auth_05_a_key_created_after_a_miss_works_at_once[sqlite]` | Distinct random prefixes still cost one ledger read each. There is no per-peer throttle. |
| SR-12 | **Closed** | `.github/workflows/ci.yml:35` `uv lock --check`; pin lint in `tests/unit/test_pins.py` | `test_pins.py::test_t_sup_01_*` (3), `::test_t_sup_02_ci_checks_that_the_lockfile_matches_pyproject`; `uv lock --check` passes locally | |
| SR-13 | **Partly closed** (→ NF-02) | `pipeline/pipeline.py:271-278` (buffered: `asyncio.timeout`, 504, TIMEOUT means the full hold if sent); `pipeline/streaming.py:166-169` (stream deadline checked in `_step`) | `test_review_fixes.py::test_sr_13_a_call_that_runs_too_long_is_timed_out_at_the_full_hold`, `::test_sr_13_a_stream_that_runs_too_long_is_broken_with_usage_if_seen_else_the_hold` | The stream deadline is enforced only when the client pulls the next chunk (NF-02). |
| SR-14 | **Closed** | `ledger/hold_flow.py:174-183` (explicit `LedgerError`) | `test_review_fixes.py::test_sr_14_state_machine_invariants_are_not_guarded_by_assert[ledger/hold_flow.py, pipeline/settle.py]` | A grep finds no `assert` statement left under `src/`. |

## New findings

### NF-01: one key can fill the process-wide gate with three chunked uploads (Medium, verified by probe)

- **Where:** `pipeline/intake.py:23,26,86`; `pipeline/pipeline.py:95-100,156-158`; `pipeline/steps.py:285-297`.
- **What happens:** a JSON request without `Content-Length` is charged as a maximal JSON body times the amplification factor: 4 MiB × 32 = 128 MiB. The default gate is 384 MiB, so three such requests fill it.
  - The per-key intake ticket does not prevent this. It follows the key's concurrency limit, which is 16 in the reference configs (`deploy/compose/spendgate.*.yaml:44`, `examples/config.yaml:50`) and higher than the gate's capacity.
  - Every other tenant then gets `429 "The gateway is at its concurrency limit"` for up to `body_read_timeout_seconds` (30 s). Repeating every 30 s makes it a standing cross-tenant denial of service by any key holder.
  - It is also a capacity regression for honest clients: before the fix, a chunked request weighed 75 MiB against a 600 MiB budget (8 at once); now only 3 fit. A 4 MiB body with `Content-Length` weighs the same, and a 1 MiB long-context request weighs 32 MiB (12 per process).
- **Evidence:** probe with `open_stack(max_concurrent=16)` and the defaults (gate 384 MiB, JSON cap 4 MiB). Three stalled chunked uploads from key A gave `gate.in_flight == 3`. A 70-byte request with `Content-Length` from a second key B was then refused with 429.
- **Suggested fix:** for JSON, weight by bytes actually buffered instead of the worst case. Alternatively, cap one key's share of gate memory (for example, at most one maximal-weight intake per key) or give requests without `Content-Length` their own small pool. Add a test where a second key is still admitted while one key holds stalled uploads.

### NF-02: the stream deadline, kill switch and session stop need the client to keep reading (Low–Medium, verified by probe)

- **Where:** `pipeline/streaming.py:159-169` (checks only in `_step`); `pipeline/leases.py:56-63` (`renewed` renews every watched id); `pipeline/upkeep.py:29-45`.
- **What happens:** `max_request_seconds`, `abort()` (kill switch, session time, lease lost) and `near_expiry` are only acted on when the adapter pulls the next chunk.
  - When a client stops reading, uvicorn's `send` blocks on flow control. `_step` then never runs, while the heartbeat keeps renewing the id because it is both "owned" and "watched".
  - The hold, a key concurrency slot and an upstream connection stay pinned past the 900 s limit for as long as the TCP connection lives.
- **Evidence:** probe. A stream opened through `pipeline.serve` with `max_request_seconds=120` was never iterated. After 60 heartbeats (1200 s) with `renew_leases()`, `watch_streams()` and `sweep_expired()` each beat, the reservation was still `pending`, with its lease 60 s ahead and totals `(0, 55000)`. It settled only on `aclose()`.
- **Suggested fix:** enforce the deadline from the heartbeat side. Stop renewing (and `aclose()`/settle) streams whose `held_at + max_ms` has passed or that were aborted. For example, `Leases.renewed` could exclude expired streams, and `abort()` could schedule `_settle` when no `_step` is in progress. Add a test where the stream is never iterated.

### NF-03: a lost hold commit now charges a call that was never sent (Low, verified by probe)

- **Where:** `pipeline/pipeline.py:230-244` (`_hold`: a non-conflict exception gives `_LEDGER_DOWN` and the id is never added to `_owned`).
- **What happens:** when the hold transaction commits but the reply is lost (the Postgres ambiguous-COMMIT case named in CR-02), the hold is no longer renewed. The sweeper then charges it in full, even though nothing was sent upstream.
  - Before the fix the same hold was locked forever. The new outcome fails closed, but it contradicts "free only if never sent" (T-MON-11).
  - The same applies to `_give_back` (`:246-258`) when its single release attempt fails.
- **Evidence:** probe with a ledger wrapper whose `hold` commits and then raises `LedgerUnavailable`. The client got `503 ledger_unavailable`, with 0 upstream requests and totals `(0, 55000)`. After five heartbeats and a sweep the totals were `(55000, 0)`: charged in full.
- **Suggested fix:** after an ambiguous `hold` failure, attempt a best-effort `release(request_id, (), now)`. It is state-guarded and harmless if nothing committed. Or queue it in `Closer._unsettled` as a release decision, so the heartbeat retries it before the lease runs out.

### NF-04: an exception in the stream close path leaves the hold renewed for the life of the process (Low, verified by fault injection; no natural trigger found)

- **Where:** `pipeline/streaming.py:280-286`. `_close` runs `_record` before `env.close`. Only `Pipeline._close` (`pipeline.py:290-297`) discards the id from `_owned`.
- **What happens:** if `_record` raises (for example from `read_usage`, `attempt_record` or `live.broke`), `env.close` never runs. The id stays in `_owned`, and the heartbeat renews it indefinitely. This is the CR-02 failure mode, on the stream path. The buffered path has a guard (`handle`'s `except` at `pipeline.py:200-203` discards the id).
- **Evidence:** with `streaming.read_usage` patched to raise `TypeError`, after 200 s of heartbeats the state was `pending`, the lease 60 s ahead, totals `(0, 55000)` and `rid in _owned == True`. A malformed `Usage` from the relay did **not** trigger it (that settled at the full hold), so no realistic trigger is known.
- **Suggested fix:** wrap `_record` in `try/finally` so `env.close` always runs. With an in-flight `book.flight`, `decide` already charges the full hold. Or discard from `_owned` in `StreamBody._settle`'s failure path.

### NF-05: audio measurement still blocks the event loop for seconds (Low; residual of SR-03, not a regression)

- **Where:** `core/audio.py:364-409` (`_matroska` runs synchronously on the event loop).
- **What happens:** the parser is now linear, but a 24 MiB WebM made of empty Void elements measured **6.8 s** in `measure()` (12 M iterations). That is about 7 s of stall for every tenant per upload. The default gate admits about five transcription uploads at once.
- **Suggested fix:** run `measure` in a worker thread with a time budget, or cap the element count per file.

### NF-06: documentation drift after the fixes (Low, docs)

- `docs/threat-model.md:75` (TH-05 residual) still says "the body is read before the key is checked … T-DOS-04 is planned, so treat this as an open residual".
- `docs/threat-model.md:158` (R-05) says "Open until their fixes land: … (SR-01, SR-02 …)". Both fixes landed in a665a46.
- `ledger/key_types.py:32` says `allowed_models` "empty is stored as None (= every model)". Empty is now refused.
- **Suggested fix:** update all three texts. Tag the SR-02 tests `T-DOS-04` and the SR-01 tests `T-DOS-01`/`T-DOS-03` where they apply.

### Checked and found sound

- No upstream contact without a `Held`: `Forwarder.run` is called only from `_forward_and_settle`, after the hold.
- `decide` charges the full hold whenever a flight is open: on timeout (`TIMEOUT`), on cancel, and on any exception between send and `_record`.
- Exactly one transition on every stream ending: done, cut, limit, lease lost, deadline (when pulled), client gone, and `aclose` at shutdown.
- `_book` retries are idempotent (state-guarded flows).
- A late settle after the sweeper books only the overrun. A late release after expiry is a no-op that the closer treats as booked.
- The intake ticket is released idempotently by ticket number.
- `retry_unsettled` uses one try with no sleep inside the heartbeat.
- `renew_flow` renews only ids that are named, pending and owned by this instance. Test: `test_renew_all_extends_only_that_instances_pending_holds`.

## Gate results observed (`tests/fuzz/` excluded)

- `ruff check`: all checks passed. `ruff format --check`: 266 files already formatted.
- `mypy --strict src tests scripts`: no issues in 273 source files.
- `lint-imports`: 6 contracts kept, 0 broken.
- `pytest -m "not live and not load and not fuzz"` with coverage: **1768 passed, 178 skipped, 10 deselected** (80.9 s). Coverage **94.60%** (≥ 90%). The skips are Postgres (no `SPENDGATE_TEST_POSTGRES_URL`) and Docker perimeter tests.
- The 28 test files touched by a665a46 run alone: 461 passed, 82 skipped (Postgres, Docker).
- `detect-secrets`: clean. `pip-audit --strict` (locked set): no known vulnerabilities. `uv lock --check`: OK.
- `scripts/lightweight_report.py`: OK. 0 modules over 400 lines (non-blank count; `pipeline.py` has 406 physical lines), 0 functions over 50, 5 base dependencies (max 6).
- **Not verified:** the Postgres paths, including the per-id `RES_RENEW` and renew/sweep on Postgres, and the Docker perimeter and block tests. Both need a live Postgres and Docker. Running `make test-postgres` and `pytest -m docker` would close this gap.
- Probes (temporary scripts outside the repository, driving `tests/integration/harness.py`):
  - NF-01: gate fill;
  - NF-02: stream never read;
  - NF-03: lost hold commit;
  - NF-04: stream `_record` fault injection, plus a malformed-`Usage` control that settled correctly;
  - NF-05: `measure()` timing;
  - SR-07: direct calls to `rfc3339_ms`, `parse_time` and pydantic `KeyCreateIn`/`KeyUpdateIn`.
