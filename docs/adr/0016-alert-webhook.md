# ADR-0016: Budget-warning webhook

**Status:** Accepted
**Date:** 2026-11-23

## Context

Budget warnings (`soft_threshold`, `cap_breached`, `blocked`) are WARNING lines on the
`spendgate.budget` logger, written from what the ledger's `hold` and `settle` return. An operator
who wants them pushed somewhere must scrape logs. Issue #30 asks for an optional webhook. It adds
outbound traffic to a host that is not a provider and a new config section, so it needs a
decision: where the post happens, what it carries, how it is authenticated and what happens when
the receiver is slow or down.

## Decision

- New optional config `alerts.webhook` with `url` and `signing_secret`, both secret references
  (`env:NAME`, `file:/path`): a webhook URL often carries a token. The URL follows the provider
  `base_url` rule: `https://`, or `http://` only for localhost. It is set by the operator only,
  never from a request, and redirects are not followed. `timeout_seconds` (5), `max_attempts` (5,
  at most 10) and `queue_size` (1000) bound the work. Unset, nothing changes.
- The webhook is a second handler on the `spendgate.budget` logger (`gateway/alert_webhook.py`),
  attached by `spendgate serve`. The body is the log record's fields passed through the same
  allowlist as the JSON log (`logs.redact`), plus `event_id` (`sg_evt_...`) and `ts`. One source
  of fields means the body cannot carry more than the log line. The pipeline is unchanged.
- The handler only queues, in a bounded in-memory `asyncio.Queue`. A full queue drops the new
  warning; drops, failures and deliveries are counted in
  `spendgate_alert_webhooks_total{result}`. One background task, run beside the sweeper, posts in
  order. A network error, a timeout (bounding the whole attempt), a 429 or a 5xx is retried with
  backoff 1, 2, 4, ... s capped at 30 s, through an injected sleeper; any other status is final.
  A give-up writes one `alert_webhook_failed` line with the error class or status, never the URL.
- `X-Spendgate-Signature: sha256=<hex>` is HMAC-SHA256 of the exact body bytes with
  `signing_secret`. The id and time are inside the signed body, so a receiver can drop replays.
- Library mode loads and validates the section but does not post: it has no long-running task
  of its own to own a queue.
- No new dependency: `httpx` and the standard library (`hmac`, `hashlib`).

## Consequences

- Warnings are delivered at most once and may be lost: a full queue, a receiver down for longer
  than the retries, or a stop with warnings queued. The ledger (`soft_alerted_at`,
  `review_required_at`) stays the record, as for the log line.
- A slow or failing receiver delays only later warnings, never a call: nothing on the request
  path awaits the network, and the handler never raises.
- One more outbound destination (trust boundary B6 in the threat model). It receives budget facts
  only, the same as the log.
- Not done: several receivers, per-event filtering, Slack or email formats. A receiver can filter
  on `event`.
- Documented in [operations.md](../operations.md#budget-warning-webhook) and
  [configuration.md](../configuration.md#alertwebhook).
