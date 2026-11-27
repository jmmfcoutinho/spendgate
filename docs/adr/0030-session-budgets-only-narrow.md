# ADR-0030: Session budgets only narrow, and `keys:write` cannot hand out spend room

**Status:** Accepted
**Date:** 2026-11-27

## Context

The admin API splits duties into scopes (threat model C-04): `keys:write` issues and changes keys,
`budgets:write` creates and raises budgets and engages kill switches. A security review found two
ways for a `keys:write` key to undo what only `budgets:write` should decide:

- **A session budget stood in for a budget.** Admission fails closed when no budget applies to a
  call (`no_budget`, ADR-0003), but it counted a session's own budget as one. That budget comes
  from the key's `session_defaults.budget_nano`, which `keys:write` sets. So `keys:write` could
  create a new top-level scope (no budget), a key on it with a large session budget and no
  new-session bound, and spend without limit by naming a new session on each call. No budget row
  showed up for the scope.
- **Rotation shed a key kill switch.** A `key` kill switch matched one key id. Rotation issues a
  replacement with a new id and the same scope, models and limits, so rotating a killed key lifted
  the switch in effect.

[How money is protected](../how-money-is-protected.md), guarantee 6, already described a session
budget as a cap inside the shared budgets, never as one of them.

## Decision

- **A session budget only narrows (M-17).** A call is admitted only when at least one active budget
  on a non-session scope of the key's chain applies to it (its model filter matches). If none does,
  the call gets the existing `forbidden` refusal with `details.reason` `no_budget`, exactly as when
  the chain has no budget at all. A session's own budget is still checked and held, and still
  refuses with `session_limit_reached` when it is the tighter one. A budget counts as shared by the
  kind of the scope that owns it, so a key placed on a session scope gets no room from that
  session's budget either. The check runs in the admission transaction after the budget rows are
  locked and before any verdict; there is one flow for both backends, so SQLite and Postgres
  behave the same.
- **`keys:write` alone may only narrow what sessions can spend.** Creating or patching a key also
  needs `budgets:write` when it sets or raises `session_defaults.budget_nano`, or raises
  `max_new_sessions_per_hour` or sets it to `null`. The change is compared with the stored value;
  a key without session defaults counts as the defaults a new key gets (no session budget, 60 new
  sessions an hour). Without `budgets:write` the answer is the usual `forbidden` (403), with
  `param` naming the field. Lowering either value, and every other session limit, still needs only
  `keys:write`. This is defence in depth: after the first point a session budget cannot add spend
  room, but the key-issuing role should not be able to grant room even if a later change broke
  that rule. Clearing a session budget is not covered: without the first point, a session with no
  budget on a chain with no budget was already refused.
- **A key kill switch survives rotation (M-18).** A `key` switch applies to the key it names and to
  every key that replaces it by rotation, however long the chain (`replaces_key_id`, followed in
  the admission transaction in the same batch as the switches). Lifting that one switch lifts it
  for the whole chain. A switch on a replacement does not reach back to the key it replaced, which
  expires at the end of its grace period. We chose matching over copying the switch onto the new
  key at rotation: `replaces_key_id` is written once, when the replacement is inserted, so the
  lineage cannot change under a running call and there is no window in which a switch engaged
  during a rotation is missed; copying would need a second write in the rotation transaction,
  could miss a switch engaged concurrently on Postgres, and would leave a second switch to lift.
  The hold returns the lineage so the heartbeat stops an open stream of a rotated key too.

## Consequences

- A deployment whose only limit on some scope chain was the session budget now gets `no_budget`
  on that chain, with or without the session header. Add a budget on the key's scope or an
  ancestor; the session budgets keep narrowing it.
- An admin key with `keys:write` but not `budgets:write` can no longer create keys with a session
  budget or a looser new-session bound, nor raise them. Give such changes to a key that also holds
  `budgets:write`, or create the key without them and let a `budgets:write` holder raise them.
- Rotating a killed key yields a killed key. To hand out a fresh key that works, lift the switch
  first (`budgets:write`).
- No schema change, no new error code, no new endpoint. Admission reads one more small recursive
  query, in the batch it already sends.
- Not changed: a `key` switch stops a credential, not a scope. A `keys:write` holder can still
  create a new key on the same scope. To stop spending on a scope whatever key is used, engage a
  `scope` switch or lower the scope's budget.
