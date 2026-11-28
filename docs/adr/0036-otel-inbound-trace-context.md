# ADR-0036: A caller's trace context is continued only when the operator trusts it

**Status:** Accepted (amends [ADR-0019](0019-otel-extra.md), tracing)
**Date:** 2026-11-28

## Context

ADR-0019 made the gateway continue a valid inbound W3C `traceparent`: the caller's span is the
parent of the hold, forward and settle spans, and the provider request carries a `traceparent` in
the caller's trace. That was what issue #34 asked for, so a call shows up inside the caller's own
trace.

A `traceparent` is made by the caller. It names a trace id and carries a sampled flag. With a
parent-based sampler, which is the OpenTelemetry default (`parentbased_always_on`) and the usual
way to sample by ratio (`parentbased_traceidratio`), the parent's flag decides whether the gateway
records the call. A security review measured it (pen test T-083, issue #142): with
`parentbased_traceidratio` at ratio 0, ten untraced calls gave 0 spans and ten calls with a sampled
`traceparent` gave 30, all in the caller's trace id, and that trace id reached the provider on
every request. So any key holder can:

- force tracing of their calls whatever ratio the operator chose, raising span volume (and the
  bill of a hosted tracing backend) at will;
- with an unsampled flag under the default sampler, keep their calls out of the operator's traces;
- choose the trace id the provider logs for the gateway's requests, and so plant or correlate ids
  in logs the operator does not control.

No secret leaks: spans carry only the allowlisted attributes, and `tracestate` and baggage are
already dropped. ADR-0019 implied this behaviour but did not spell it out, and its consequence that
the trace id the provider sees "carries nothing about the caller" is false when the caller picked
it.

spendgate is deployed two ways. In front of callers the operator does not fully trust (agents,
third-party apps, other teams with their own keys), which is the case its budgets exist for, a
caller should not steer the operator's telemetry. Inside a traced service mesh, where the callers
are the operator's own services, joined traces are the point of tracing.

## Decision

- **A new setting, `observability.otel.trust_inbound_context` (boolean, default `false`).** When
  false, the gateway ignores an inbound `traceparent`: every call starts a new root trace at its
  hold span, the operator's sampler decides alone, and the provider gets a `traceparent` naming the
  gateway's own forward span in the gateway's own trace, as for a call that sent none. When true,
  the behaviour of ADR-0019 holds: a valid `traceparent` is the parent of the three spans and its
  trace id reaches the provider.
- **Off by default.** spendgate refuses by default what it cannot vouch for (ADR-0003), and the
  caller is who the gateway guards against. An operator who wants joined traces says so with one
  line; an operator who does not would otherwise get caller-decided sampling without being told.
- **`tracestate` and baggage are unchanged:** never read and never passed on, trusted or not.
- **No span link to the caller's context.** Linking the ignored `traceparent` (as OpenTelemetry's
  "public endpoint" option in some instrumentations does) would keep a pointer to the caller's
  trace without letting it decide sampling, but it would put a caller-chosen id into every exported
  span and add a span feature the allowlist review (T-OBS-04) does not cover. It can be its own
  decision if operators ask for it.
- The pipeline is unchanged: the choice lives in the `gateway.otel` adapter behind the `Tracer`
  port, whose `parent()` returns "no parent" when the context is not trusted.

## Consequences

- **Behaviour change on upgrade:** an operator with tracing on whose callers send `traceparent`
  stops seeing the gateway's spans inside those callers' traces; each call is its own trace, and
  the provider no longer sees the caller's trace id. Set `trust_inbound_context: true` to keep the
  old behaviour. Money, routing and the call log are not affected, and nothing changes with
  tracing off (the default) or without the `otel` extra.
- With the setting true, the caller decides sampling: under a parent-based sampler by its sampled
  flag, and under `traceidratio` by the trace id it picks, since that sampler computes the ratio
  from the trace id. Only `always_on` and `always_off` take the choice away, so the setting is for
  callers the operator trusts with it.
- A gateway span's request id still matches the call log and the `calls` table, so a call can be
  found from either side without a joined trace.
- No ledger, endpoint, error-code or dependency change. Test id T-OBS-05.
