# How to add a provider adapter

An adapter teaches spendgate one provider's wire format. It does not touch money: the pipeline
holds, sends, settles and refunds; the adapter only validates, translates, reads usage and maps
errors. That keeps an adapter small, and a new one cannot weaken a money rule unless it reports
usage wrongly. Read [architecture](architecture.md) and the [wire protocol](protocol.md) first.

## The port

An adapter implements `Provider` in
[`src/spendgate/providers/port.py`](../src/spendgate/providers/port.py): plain data in, plain data
out, no HTTP client.

| Member | Job |
|---|---|
| `name` | The provider slug; equal to the module name |
| `validate(req)` | Pure checks of endpoint, body, headers and files against an allowlist, once, before routing. Returns a refusal or `None` |
| `translate(req, deployment)` | Build the upstream request: URL is the configured base plus a fixed path, never anything from the client; adds the credential. Raises `Untranslatable` for a request this provider cannot carry |
| `response(req, resp)` | A non-streamed answer in the client's shape (identity unless you convert) |
| `relay(req, limits)` | A per-request `StreamRelay` that turns upstream bytes into client bytes and remembers usage, enforcing `SseLimits` |
| `usage(req, resp)` | Normalised `Usage` of a non-streamed answer, or `None` if absent or unreadable |
| `error(resp)` | Map an error answer to an `UpstreamError` (`sent`, `status`, `retry_after_seconds`) |

Rules the pipeline relies on:

- **Usage is normalised into core `Usage`:** cached and cache-write tokens are subsets of
  `input_tokens`, reasoning tokens a subset of `output_tokens`, audio counters separate. If you
  cannot tell, resolve to the dearer meter. Return `None` when usage is missing: the pipeline then
  charges the full hold. Never invent a number.
- **`UpstreamError.sent` is `False` only for failures before the request left** (DNS, connect,
  TLS). Those are free and fail over. Anything else is charged by the settlement rules
  ([how money is protected](how-money-is-protected.md), M-13).
- **Throttling statuses** that mean "rejected before processing" are not decided here: they come
  from `providers.<slug>.free_statuses` in the catalog, and may only be 429 or 529.
- **Errors never echo user content or secrets.** Messages name the field, not the value. Reprs of
  deployments and requests must not show credentials.
- **Hostile responses are expected.** A stream that is malformed or too large raises
  `StreamBroken` or `StreamLimitExceeded`; it is settled at the usage seen or the full hold.
- **Refuse, never drop.** A request field you cannot translate is `invalid_request` with `param`,
  not silently ignored.

## Steps

1. **Slug and module.** Pick a lowercase slug (`my_provider`) and create
   `src/spendgate/providers/my_provider.py`. Helper modules must be prefixed with the slug
   (`my_provider_stream.py`). `providers` may import only `core`, and adapters never import each
   other (`uv run lint-imports` enforces it). The shared SSE codec is `providers/sse.py`.
2. **Register it.** The slug appears in four places: the `PROVIDERS` tuple in
   `src/spendgate/core/catalog.py`, `ProviderSlug` in `src/spendgate/config/schema.py`,
   `build_providers` in `src/spendgate/pipeline/adapters.py`, and the expected sets in
   `tests/unit/test_layout.py` and `tests/unit/test_catalog.py`. If it needs a new `auth` mode,
   extend `AuthMode` in the port and `AuthName` in the schema.
3. **Price it.** Add catalog entries for its models and a `providers.<slug>.free_statuses` block
   ([catalog how-to](catalog-how-to.md)). Without a price the model is refused.
4. **Record real answers.** Save sanitised request and response samples, with the date, under
   `tests/contract/providers/<slug>/fixtures/` (for example `message_response_20261117.json`) and
   write contract tests beside them: allowlist accepted and refused, headers, usage for plain and
   cached cases, streaming (including a cut stream and a hostile one), error mapping, and a test
   that no secret appears in a repr or error. Existing adapters under
   `tests/contract/providers/` are the template.
5. **Teach the fake.** `tests/fakes/` speaks each provider's wire format so the whole stack runs
   without a network. Add a shape for yours in `tests/fakes/shapes_*.py` and register it in
   `tests/fakes/shapes.py`, then add a gateway test that sends a call through it.
6. **Document it.** Add the endpoint to the table in [protocol.md](protocol.md) and the matrix in
   the [README](../README.md). A new slug or a new config value is public behaviour: write an ADR
   in [`docs/adr/`](adr/).

## Size limits

Each adapter file is at most 400 non-blank lines, and functions about 50 (CONTRIBUTING sets both;
the adapter limit is a hard check). Split a big adapter into `<slug>_shapes.py` and
`<slug>_stream.py` rather than growing one file, as the OpenAI and Bedrock adapters do. Check:

```bash
uv run python scripts/lightweight_report.py
```

Then run the gates: `make check`. The layout test (`tests/unit/test_layout.py`) fails if a
provider module lacks its port or a helper does not name its adapter.
