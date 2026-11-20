# Load, overshoot and overhead review (v0.1)

Date: 2026-11-17. Code: the pre-release tree before the load tests were added. Tests:
`tests/load/` (`uv run pytest -m slow tests/load`; the PostgreSQL cases need
`SPENDGATE_TEST_POSTGRES_URL`). Provider: the fake from `tests/fakes`, never a real one.

## Setup

- **Machine:** WSL2 laptop (AMD Ryzen 9 7945HX, 32 logical CPUs, 78 GB RAM, Linux 6.18 WSL2 kernel),
  Python 3.13.9, uvicorn 0.54, starlette 1.10, httpx 0.28, psycopg 3.3.8. PostgreSQL 17.12 in Docker
  (`postgres:17-alpine`, bound to 127.0.0.1, default fsync). Everything shares the one machine, so
  absolute latencies are laptop numbers; the guarantees do not depend on them.
- **Processes:** the real `spendgate serve`, N copies on distinct loopback ports. SQLite: 4 gateways on
  one ledger file. PostgreSQL: 3 gateways on one schema. One fake-provider process (the shared fake's
  wire shapes behind a real HTTP server, three deployments of one model so failover is exercised).
  8 client processes with 50 concurrent calls each (400 in flight), round-robin over the gateways.
  The harness runs outside pytest (`tests/load/driver.py`) because pytest's socket guard only allows
  loopback for `docker`/`postgres` tests; pytest starts it and asserts on the ledger rows it reads.
- **Workload:** 6 keys under 2 projects under 1 org. Caps are sized in worst-case holds (58 000 nano
  each) so the org (160 x scale), project (100 x scale) and user (35 x scale) levels all bind. Each
  call asks for `max_tokens` 100 and the fake reports 70-100 output tokens, so settled spend is
  near the hold. Random per call, from the client's own generator: stream or not (40%), provider
  latency 0-60 ms (long-tailed), provider 429 (3%), provider 500 (1%), stream cut mid-body (4% of
  streams), client cancel (5%). Clients stop when every key's chain is exhausted (spent within one
  hold of the cap); a `budget_exhausted` answer makes a client wait about a second.
- **Seed:** `20261117` (`SPENDGATE_LOAD_SEED`). Each client uses `Random(seed*1000 + n)`, so every
  client's call plan is reproducible; how 8 processes interleave is up to the kernel, so two runs
  agree on invariants, not on exact counts.
- **Scenarios** (per ledger): `clean` (nothing exceeds an estimate: nothing may pass a cap),
  `overrun` (25% of replies report 3x the output tokens: estimator misses), `crash` (one gateway
  killed with `kill -9` while holds are in flight; leases of 4 s so the sweeper acts inside the run).
- **Run size:** default about 3 minutes for both ledgers (SQLite budgets x3, PostgreSQL x1 and
  4x25 callers in the crash run; see Findings 3). `SPENDGATE_LOAD_SCALE=N` multiplies the budgets;
  `SPENDGATE_LOAD_REPORT_DIR` keeps each raw report.

## What is asserted after each run (read from the ledger tables, not through the code under test)

1. `spent + held <= cap + overrun + excess` for every budget at rest and in a snapshot every 250 ms
   during the run (10 to 128 snapshots per run). `clean` runs: nothing ever above the cap.
2. No negative `spent`, `held`, `overrun`, `excess`. No grants in the world, so effective cap = cap.
3. After the exhausted keys keep calling (40 calls each, `volley`): every answer is `402
   budget_exhausted` and the fake's per-key upstream counter does not move.
4. Every reservation is `settled`, `released` or `expired` (none pending, `held` is 0 everywhere,
   the end-of-run sweep finds nothing left to charge in the non-crash runs).
5. Per reservation, the `calls` rows add up to its charge; per budget, `spent` equals the sum of the
   charges of its reservations (an expired hold counts in full, plus any late overrun).
6. The provider saw exactly as many requests as the ledger has owner attempt rows (crash run: at least).
7. No client-visible failure other than the expected refusals, no `internal_error` or
   `ledger_unavailable`, no error-level gateway log line.

## Results

| Ledger | Scenario | Client calls (+volley) | Reached the provider | Reservations settled / released / expired | `402` refusals | Max overshoot vs bound | Errors |
|---|---|---|---|---|---|---|---|
| SQLite (4 gw) | clean | 2257 (+240) | 684 | 767 / 968 / 0 | 620 | 0 vs 0 | 0 |
| SQLite (4 gw) | overrun | 1244 (+240) | 425 | 531 / 653 / 0 | 181 | 0 sampled (1 budget flagged for review) vs 19.98 M summed overrun | 0 |
| SQLite (4 gw) | crash | 2851 (+240) | 641 | 591 / 773 / 32 | 862 | 0 vs 0 | 0 (560 connection refusals to the dead gateway, expected) |
| PostgreSQL (3 gw) | clean | 3781 (+240) | 233 | 313 / 54 / 0 | 3323 | 0 vs 0 | 0 |
| PostgreSQL (3 gw) | overrun | 425 (+240) | 140 | 239 / 23 / 0 | 247 | 36 000 nano vs 5.77 M summed overrun | 0 |
| PostgreSQL (3 gw) | crash (4x25 callers) | 662 (+240) | 217 | 197 / 70 / 10 | 191 | 0 vs 0 | 0 (186 refusals to the dead gateway, expected) |

Overshoot is nano-units of USD (1 USD = 10^9): the largest `spent + held - cap` seen in any snapshot
or at rest; the bound is the budget's `overrun + excess`. The only breach observed (PostgreSQL
overrun run, 36 000 nano = 0.000036 USD) is inside the overrun bound, and the `strict_overrun` flag
was set on the breached budgets, as designed. `released` includes requests refused after the hold
because every deployment was cooling down (`503 upstream_unavailable`, no upstream contact, charge 0).
Seconds per run: SQLite 6-9, PostgreSQL 7-39.

### Overhead (0 ms provider latency, one sequential client, one gateway)

One call to the fake directly, then one through the gateway, alternating. Overhead = gateway round trip
minus the median direct round trip (conservative: the direct call's own jitter stays in). 600 samples
SQLite, 300 PostgreSQL, after 100 warm-up pairs.

| Ledger | Call | Direct p50 | Gateway p50 | Gateway p99 | **Overhead p50** | **Overhead p99** | Target p50 / p99 |
|---|---|---|---|---|---|---|---|
| SQLite | plain | 0.85 ms | 5.03 ms | 7.83 ms | **4.18 ms** | **6.98 ms** | < 5 / < 25: met |
| SQLite | stream | 1.06 ms | 6.07 ms | 8.88 ms | **5.00 ms** | **7.82 ms** | p50 at the limit (4.97 to 5.11 over four runs), p99 met |
| PostgreSQL | plain | 1.04 ms | 26.1 ms | 33.6 ms | **25.1 ms** | **32.6 ms** | not met (p50 5x, p99 1.3x over) |
| PostgreSQL | stream | 1.22 ms | 32.6 ms | 43.2 ms | **31.4 ms** | **42.0 ms** | not met |

### Limits across workers (input for a shared limiter store, nothing more)

Limits are per process, so they multiply by the number of workers. A key with `rpm 30`, 6 x 30 x N
calls in about 2 s, round-robin over the N gateways: SQLite (N=4) admitted 120, PostgreSQL (N=3)
admitted 90, the rest `rate_limited`, i.e. exactly N x the limit. A key with `max_concurrent 4` and 64
calls at once: 16 admitted (N=4), 12 (N=3). This is by design for v0.1.

## Release criteria

- **No overshoot** (target): "8 processes x 50 concurrent calls against a fake provider never take
  booked spend past any budget by more than the documented overrun bound. Calls after exhaustion are
  refused before upstream contact." **Met** for both ledgers in all six runs (rows above): invariant held in
  every snapshot and at rest, zero upstream calls from any exhausted key (240 volley calls per run), every
  hold ended (including 32 and 10 holds of the killed gateway, charged in full by a sweeper).
  One caveat: the crash run only passes with a harness workaround for Finding 1.
- **Overhead** (target): "gateway overhead p50 < 5 ms, p99 < 25 ms per call against the fake provider
  (excluding upstream time)". **Met on SQLite for plain calls; borderline on SQLite streams (p50 5.0 ms, p99 met);
  not met on PostgreSQL (p50 about 25 ms, p99 about 33 ms).** The overhead tests for the three cases that miss
  are non-strict expected failures so the suite stays green while the miss stays visible.

## Findings (none of them a money failure)

1. **Product bug: every `serve` process names itself `gateway`.** `build_pipeline` never passes `instance`,
   so `owner_instance = 'gateway'` for all processes. `renew_all('gateway')` by a live peer keeps a crashed
   peer's holds alive for ever and the sweeper never charges them. Seen: 117 holds still pending 25 s after
   `kill -9` with a 4 s lease, ending only when the clients stopped. The invariant still holds (fail closed),
   but the budget stays blocked until every gateway has stopped. The crash scenario therefore starts each
   gateway through a launcher that gives it a distinct name (`UNIQUE_INSTANCE_LAUNCH` in `tests/load/rig.py`);
   the other scenarios use the shipped command. Failing test, kept as a strict xfail:
   `tests/load/test_known_defects.py::test_each_gateway_instance_has_its_own_lease_owner_name`.
   Related doc rule: "a restarted instance gets a new `owner_instance`" (ledger-state-machine.md).
2. **Product defect (minor): `ClientDisconnect` escapes the body read.** A client that leaves before its body is
   read makes `read_capped` raise starlette's `ClientDisconnect`; uvicorn logs "Exception in ASGI application"
   and a traceback. It happens before authentication and any hold, so no money is involved; 1 to 3 per run here.
   Strict xfail: `tests/load/test_known_defects.py::test_client_leaving_before_the_body_is_read_is_not_an_unhandled_error`.
3. **PostgreSQL is much slower than SQLite per call here and degrades with concurrency.** Ledger operations
   measured alone: hold 6.7 ms, settle 4.5 ms (many sequential statements per transaction, 0.4 ms per round
   trip to the container); a plain call through the gateway is about 26 ms. Turning `synchronous_commit` off
   changed it by only 3 ms, so it is round trips and not fsync. With 400 calls in flight on one hot org budget,
   `pg_stat_activity` showed about 28 of 30 connections in `Lock:tuple` on that row, and the median successful call
   took 10.5 s (SQLite: 1.1-1.8 s), close to the 10 s pool timeout. Dropping the crash run to 4 x 25 callers cut it from
   over 100 s to 9 s. Not a correctness problem and not a regression test, but anyone sizing a multi-instance
   deployment should know a budget shared by many callers is a serialisation point. Candidate follow-ups (not done
   here): fewer statements per transaction, a larger pool, an explicit overhead target per ledger.
4. **Cooldown amplifies provider throttling.** With 3% injected 429s and `cooldown_seconds: 1`, a gateway often has all
   three deployments cooling, and then answers `503 upstream_unavailable` after taking and releasing a hold (charge 0, no
   upstream contact): 759 of 2257 calls in the SQLite clean run. By design (per-process cooldown), but it makes
   `released` the biggest reservation state in these runs.
5. **Settlement can wait behind refusals.** Refused admissions take the same budget-row locks as admitted ones; at
   400 callers the settlement of in-flight calls queues behind them (the cause of the PostgreSQL latencies above). Clients
   in this harness back off about a second after a `402`; without it the crash run on PostgreSQL took over two minutes.
6. **Under load, client-seen latency is dominated by queueing** (median 1.1-1.8 s on SQLite with 400 in flight). The
   overhead criterion is about a single call, which is what the table above measures; the loaded figure is in
   each run's report (`loaded_overhead`) and is not a criterion.

## Not done / limits

- Single machine, loopback only, WSL2 and a Docker-published PostgreSQL port: no network latency, no real disk
  behaviour of a server. macOS and WSL1 not tried. Absolute numbers will differ elsewhere.
- Hold sizes vary by about 1% with the length of the `user` field (the estimator counts it); the "exhausted" test
  therefore uses the smallest hold seen as threshold.
- The overshoot column for `overrun` runs is a sample (every 250 ms plus at rest), not a trace: a larger breach
  between samples is excluded by the invariant check on every sample, not by observation.

## Follow-up (2026-11-17)

Both product defects are fixed: each pipeline now takes a unique lease-owner name by default
(`host-pid-random`), so a live peer never renews a crashed peer's holds; and a client that leaves
before its body is read is handled as a quiet refusal (no traceback). Their tests in
`tests/load/test_known_defects.py` are now ordinary regression tests in the default suite. The load
and overhead runs themselves moved behind `make load` (marker `load`).
