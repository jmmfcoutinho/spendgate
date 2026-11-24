# How money is protected

Each guarantee below says what is promised, where it is implemented and which test proves it.
Five source files carry the money logic, plus the Compose file for the perimeter:

- [`src/spendgate/ledger/hold_flow.py`](../src/spendgate/ledger/hold_flow.py): the hold, in one transaction.
- [`src/spendgate/core/budgets.py`](../src/spendgate/core/budgets.py): the budget arithmetic and the overrun block.
- [`src/spendgate/core/outcomes.py`](../src/spendgate/core/outcomes.py): who pays for each way a call can end.
- [`src/spendgate/ledger/settle_flow.py`](../src/spendgate/ledger/settle_flow.py): settling, releasing and expiring a hold.
- [`src/spendgate/core/sessions.py`](../src/spendgate/core/sessions.py): session limits.

Money is integer nano-units (ADR-0004), never a float. Every test id below is a pytest node id;
run one with `uv run pytest <id>`, or all of them with `uv run pytest tests/docs/test_money_doc.py`,
which also fails if a cited test disappears.

## The guarantees

**1. Fail closed.** Anything the guard cannot price, identify or record refuses the call before the
provider is contacted: an unknown or unpriced model, a missing exchange rate, a missing output
ceiling, an unreachable ledger, a kill switch, a bad key. Price data cannot switch this off; the
only opt-outs are named keys under `fail_open:` in the config, logged at start (ADR-0003). The
refusals happen in the pipeline's steps before the send step ([architecture](architecture.md)).
Proof: `tests/integration/test_gateway_refusals.py::test_unpriced_model_is_refused`,
`::test_ledger_down_is_refused_without_contact`, `::test_kill_switch_is_refused`,
`::test_bad_key_is_unauthorized` (each asserts the fake provider saw no request);
`tests/unit/test_catalog.py::test_charge_zero_and_any_other_fail_open_knob_is_rejected`.

**2. Hard holds.** Before sending, the worst-case cost is held in every budget on the scope chain
in one database transaction (`hold_flow` in `hold_flow.py`); if any budget lacks room, nothing is
held anywhere. Admission enforces `spent + held <= effective cap + overrun + excess`
(`check_admission` in `budgets.py`; `invariant_holds` states it).
Proof: `tests/contract/test_ledger.py::test_scenario_01_last_euro_race_admits_exactly_one`,
`::test_refused_hold_leaves_no_trace_on_any_budget`,
`tests/contract/test_ledger_sessions.py::test_every_budget_on_the_chain_must_pass_and_all_are_held_together`,
`tests/contract/test_ledger_properties.py::TestSqliteLedgerStateful::runTest` (random operation
sequences never break the invariant).

**3. The provider can never bill what was not booked.** After the request is sent, a failure or
cancel is charged the full hold unless usage was seen; for a stream that is its final usage, never
a mid-stream running total. So is an answer whose usage lacks a required counter (input or
output tokens, Gemini's prompt count): a missing counter is unreadable usage, never a zero.
A call is free only if it was never sent,
the provider answered with a configured throttling status (429, plus 529 for Anthropic), or it
rejected the request with a 4xx and no usage (`not_processed` and `charge` in `outcomes.py`).
Proof: `tests/unit/test_outcomes.py::test_m13_cancel_after_send_is_charged_the_full_hold`,
`::test_s03_client_error_without_usage_is_free`, `::test_s07_unreachable_never_sent_is_free`,
`tests/integration/test_gateway_routing.py::test_server_error_is_not_failed_over_and_is_charged_in_full`,
`tests/integration/test_charge_after_send.py::test_cr_01_usage_that_raises_after_send_is_charged_the_full_hold`
(an adapter that raises after send is unreadable usage, never a release at zero),
`tests/integration/test_gateway_cancel.py::test_cancel_after_send_is_charged_the_full_hold`,
`tests/integration/test_missing_usage.py::test_m13_missing_usage_counter_charges_the_full_hold`
(one case per adapter and endpoint; present zeros are still a reading:
`::test_m13_present_zero_usage_counters_are_a_reading`).

**4. Overruns are recorded, never clamped, and block by default.** Usage above the hold is booked
as the provider reported it. If that breaches the cap, the budget stops admitting calls until an
operator runs `spendgate budgets ack-overrun` (`strict_overrun`, default on; `_flag_breach` in
`settle_flow.py`, the block in `budgets.py`). An overrun that stays inside the cap does not block.
Proof: `tests/contract/test_ledger.py::test_scenario_08_m03_m07_breach_overrun_is_booked_in_full_and_blocks_until_ack`,
`tests/unit/test_outcomes.py::test_m03_reported_usage_is_booked_never_clamped_to_the_hold`,
`tests/integration/test_gateway_streaming.py::test_absurd_streamed_usage_is_booked_in_full_and_flagged`.
Usage too large for the ledger's int64 cannot be booked as reported: it is treated as unreadable,
the full hold is charged and the call is flagged for review
(`tests/integration/test_charge_after_send.py::test_cr_04_usage_that_does_not_fit_the_ledger_charges_the_hold_and_flags_review`).

**5. Every hold settles exactly once.** The owner and the sweeper can race on one hold; exactly one
money transition happens. A hold whose owner died is charged in full when its lease expires
(`expire_flow` in `settle_flow.py`); a late settlement after that books only the extra as overrun.
Proof: `tests/contract/test_ledger_leases.py::test_scenario_12_t_mon_10_sweeper_first_then_owner_is_one_money_transition`,
`::test_scenario_02_crash_mid_call_is_charged_in_full_by_the_sweeper`,
`tests/contract/test_ledger.py::test_settling_twice_changes_nothing`.
The heartbeat renews only the holds the pipeline still owns (calls in flight and open streams), so a
hold whose settle failed is retried at each beat and then reaches the sweeper, never kept alive
forever: `tests/contract/test_ledger_leases.py::test_cr_02_renew_all_leaves_a_hold_the_caller_did_not_name_to_the_sweeper`,
`tests/integration/test_close_retry.py::test_cr_02_heartbeat_after_a_failed_settle_lets_the_sweeper_charge_the_full_hold`,
`::test_cr_02_an_unsettled_close_is_retried_by_the_heartbeat`.

**6. Sessions are bounded.** A session has its own budget, step limit, time limit and idle expiry
(`gate_session` in `sessions.py`, applied inside the hold transaction). A session that hit a limit
stays closed and says why; a refusal by a parent budget consumes no step.
Proof: `tests/unit/test_sessions.py::test_session_at_its_step_limit_is_refused_with_limit_steps`,
`tests/contract/test_ledger_sessions.py::test_scenario_09_session_step_limit_with_a_call_in_flight`,
`tests/integration/test_gateway_refusals.py::test_session_limit_is_refused_before_upstream`.

**7. Throttling fails over at no cost.** An attempt that was never sent, or that the provider
throttled, costs nothing and the request moves to the next deployment of the same model inside
the same reservation, so one request is never held or charged twice. If none is left the caller
gets `upstream_throttled`, not a budget error.
Proof: `tests/integration/test_gateway_routing.py::test_throttle_fails_over_inside_one_reservation`,
`::test_both_throttled_is_upstream_throttled_and_released`,
`tests/contract/test_ledger.py::test_scenario_04_m01_provider_429_fails_over_inside_one_reservation`.

**8. The perimeter.** In the reference Compose setup, apps sit on an internal network with no route
out, and only the proxy holds provider keys and joins the egress network
([`deploy/compose/compose.yaml`](../deploy/compose/compose.yaml)). The tests start the stack and
probe it: they need Docker and run in CI.
Proof: `tests/perimeter/test_perimeter.py::test_app_cannot_reach_the_internet_but_reaches_the_guard`,
`::test_no_credential_outside_the_guard`; the negative test
`::test_a_leaked_key_in_an_app_container_fails_the_check` shows the checker notices a leak.
The perimeter protects against apps; it does not protect against whoever controls the host
([threat model](threat-model.md), accepted risks).

## Money rules M-01 to M-15

The rules the ledger and pipeline must never break, each with the tests that prove it.

| Rule | Statement | Tests |
|---|---|---|
| M-01 | One reservation per client request, pending across attempts; the hold is the maximum over the candidate deployments | `tests/unit/test_budgets.py::test_m01_a_second_call_on_the_last_unit_is_refused_because_of_the_held_amount`; `tests/contract/test_ledger.py::test_scenario_04_m01_provider_429_fails_over_inside_one_reservation` |
| M-02 | A hold on an existing request id is a `conflict`; a release is final and only allowed when `not_processed` holds for every attempt | `tests/contract/test_ledger.py::test_m02_a_hold_on_an_existing_request_id_is_a_conflict_in_every_state`; `tests/contract/test_ledger.py::test_m02_release_needs_every_attempt_not_processed`; `tests/contract/test_ledger.py::test_scenario_11_m02_release_then_retry` |
| M-03 | Reported usage is always booked, never clamped; the sanity cap only flags | `tests/unit/test_outcomes.py::test_m03_reported_usage_is_booked_never_clamped_to_the_hold`; `tests/unit/test_pipeline_settle.py::test_usage_above_the_hold_is_booked_not_clamped`; `tests/integration/test_gateway_streaming.py::test_absurd_streamed_usage_is_booked_in_full_and_flagged` |
| M-04 | A late settle after expiry books `max(0, charge - held)` as overrun and updates the sweeper's row | `tests/contract/test_ledger_leases.py::test_scenario_13_m04_late_settle_books_the_overrun_and_updates_the_sweepers_row` |
| M-05 | Owner and sweeper racing on one hold produce exactly one money transition | `tests/contract/test_ledger_leases.py::test_scenario_12_t_mon_10_sweeper_first_then_owner_is_one_money_transition`; `tests/contract/test_ledger_leases.py::test_scenario_12_t_mon_10_owner_first_then_sweeper_is_a_no_op`; `tests/contract/test_ledger_leases.py::test_t_mon_10_owner_and_sweeper_race_has_one_money_transition` |
| M-06 | Effective cap is cap plus active grants; expiry records `excess` lazily; `spent + held <= effective cap + overrun + excess` | `tests/unit/test_budgets.py::test_m06_effective_cap_is_cap_plus_active_grants`; `tests/unit/test_budgets.py::test_m06_spent_plus_held_never_exceeds_cap_overrun_and_excess`; `tests/unit/test_budgets.py::test_m06_t_mon_04_grant_added_then_expired_records_excess`; `tests/contract/test_ledger.py::test_scenario_10_m06_grant_add_and_expiry_records_excess_lazily` |
| M-07 | An overrun inside the cap never blocks; a breach with `strict_overrun` blocks until `ack-overrun` | `tests/unit/test_budgets.py::test_m07_overrun_inside_the_cap_does_not_block`; `tests/contract/test_ledger.py::test_scenario_15_m07_overrun_inside_the_cap_does_not_block`; `tests/contract/test_ledger.py::test_scenario_08_m03_m07_breach_overrun_is_booked_in_full_and_blocks_until_ack` |
| M-08 | Lowering then raising a cap keeps the invariant and recomputes `excess` | `tests/unit/test_budgets.py::test_m08_lowering_then_raising_a_cap_recomputes_excess`; `tests/contract/test_ledger.py::test_scenario_16_m08_cap_lowered_then_raised_recomputes_excess` |
| M-09 | A missing exchange rate or output ceiling is refused, never defaulted | `tests/unit/test_estimate.py::test_m09_missing_output_ceiling_is_refused_naming_the_param`; `tests/unit/test_catalog.py::test_missing_fx_for_a_foreign_currency_is_refused_naming_the_rate`; `tests/unit/test_config.py::test_missing_fx_rate_refuses_start` |
| M-10 | Catalog data cannot fail open: `charge_zero` and `free_statuses` outside 429 and 529 are rejected | `tests/unit/test_catalog.py::test_charge_zero_and_any_other_fail_open_knob_is_rejected`; `tests/unit/test_catalog.py::test_free_statuses_outside_429_and_529_are_rejected`; `tests/unit/test_outcomes.py::test_m10_charge_zero_and_unknown_rules_are_rejected`; `tests/unit/test_outcomes.py::test_m10_free_statuses_outside_429_529_are_rejected` |
| M-11 | Session steps count per client request; idle expiry restarts a session; sessions need `session_defaults` | `tests/unit/test_sessions.py::test_m11_idle_expiry_restarts_with_a_fresh_step_counter`; `tests/unit/test_sessions.py::test_m11_max_new_sessions_per_hour_gates_creation_and_restart`; `tests/contract/test_ledger_sessions.py::test_a_session_on_a_key_without_defaults_is_a_programming_error`; `tests/integration/test_gateway_refusals.py::test_session_header_needs_a_session_key` |
| M-12 | Ledger unavailable means no upstream contact | `tests/integration/test_gateway_refusals.py::test_ledger_down_is_refused_without_contact`; `tests/perimeter/test_block.py::test_ledger_down_reaches_no_upstream` |
| M-13 | After the request is sent, a cancel or failure (including an adapter that raises, or a call that runs past `server.max_request_seconds`) is charged the full hold unless usage was seen (for a stream, its final usage; a required usage counter that is missing is no usage); free only if `not_processed` or a 4xx with no usage; failover only after `not_processed`, at cost 0 | `tests/integration/test_missing_usage.py::test_m13_missing_usage_counter_charges_the_full_hold`; `tests/integration/test_charge_after_send.py::test_cr_01_error_that_raises_after_send_is_charged_the_full_hold`; `tests/integration/test_request_deadlines.py::test_sr_13_a_call_that_runs_too_long_is_timed_out_at_the_full_hold`; `tests/contract/test_ledger.py::test_m13_cancel_after_send_is_charged_the_full_hold`; `tests/contract/test_ledger.py::test_m13_cancel_after_usage_was_seen_is_charged_that_usage`; `tests/contract/test_ledger.py::test_m13_never_sent_is_released_at_zero`; `tests/contract/test_ledger.py::test_scenario_19_5xx_after_send_ends_the_request_at_the_full_hold`; `tests/unit/test_outcomes.py::test_m13_cancel_after_send_is_charged_the_full_hold`; `tests/integration/test_gateway_streaming.py::test_cut_mid_stream_is_stream_broken_at_the_full_hold`; `tests/integration/test_gateway_stream_final_usage.py::test_m13_gemini_cut_after_a_running_total_is_charged_the_full_hold`; `::test_m13_gemini_complete_stream_is_charged_the_exact_usage`; `::test_m13_gemini_client_cancel_after_a_running_total_is_charged_the_full_hold`; `::test_m13_gemini_gateway_stop_after_a_running_total_is_charged_the_full_hold`; `::test_m13_openai_running_total_on_a_content_chunk_is_not_final_usage`; `tests/contract/providers/test_stream_final_usage.py::test_m13_mid_stream_usage_without_the_final_event_is_not_reported`; `tests/contract/providers/google/test_responses.py::test_m13_gemini_running_total_before_the_finish_reason_is_not_final_usage`; `::test_m13_gemini_finish_without_its_own_usage_keeps_the_running_total_untrusted`; `::test_m13_gemini_unspecified_finish_reason_does_not_make_usage_final`; `::test_m13_gemini_content_after_the_finish_makes_its_usage_untrusted_again`; `::test_m13_gemini_blocked_prompt_with_usage_is_final`; `tests/contract/providers/openai/test_responses.py::test_m13_only_end_of_stream_usage_is_final`; `::test_m13_content_after_the_final_usage_makes_it_untrusted`; `tests/integration/test_gateway_routing.py::test_provider_rejection_is_free_and_not_failed_over` |
| M-14 | The worst-case input estimate. Images: inline media other than a png, jpeg, webp or gif image is refused before routing; only parts in content positions count as images, an image label elsewhere (tool input, call arguments) is counted as text; each image is held at the catalog entry's `limits.max_image_tokens` or the conservative default, the largest documented per-image cost plus 25 percent. Text: the input-token estimate covers natural and random text: prose, code, identifiers, hex, UUIDs, base64, digits and digit grids, whitespace and control characters, CJK and emoji are counted at or above o200k and cl100k after the 25% margin (measured: at least 1.04x). Text chosen to tokenize badly (short ASCII words such as `uo`) can reach about 1.6x the margined estimate, never more than one token per UTF-8 byte; the excess is booked as overrun (M-03) and with `strict_overrun` blocks the budget until acknowledged (M-07). Tool definitions add the hidden prompt overhead. The long-context tier is never missed: the hold takes it when the margined estimate reaches 80% of the threshold (`input_tokens x 1.25 >= threshold`) or when the byte bound reaches it. Proven: text, including tool-call JSON and opaque inputs, costs at most one token per UTF-8 byte; assumed: an image costs at most the catalog image ceiling (`limits.max_image_tokens`, else `DEFAULT_IMAGE_TOKENS`) and hidden provider prompts fit the documented overheads (tools, structured output) | `tests/integration/test_inline_media.py::test_m14_non_image_data_url_in_chat_is_refused_before_upstream`; `tests/integration/test_inline_media.py::test_m14_non_image_data_url_in_responses_is_refused_before_upstream`; `tests/integration/test_inline_media.py::test_m14_non_image_media_type_in_anthropic_messages_is_refused_before_upstream`; `tests/contract/providers/test_ssrf_nested.py::test_m14_inline_media_must_be_an_image_even_with_remote_ok`; `tests/unit/test_estimate.py::test_m14_each_image_is_held_at_the_entry_ceiling_or_the_conservative_default`; `tests/unit/test_estimate.py::test_m14_an_image_label_in_anthropic_tool_use_input_is_counted_as_text`; `tests/unit/test_estimate.py::test_m14_an_image_label_in_chat_tool_call_arguments_is_counted_as_text`; `tests/unit/test_catalog.py::test_m14_every_bundled_entry_holds_at_least_the_documented_image_cost`; `tests/unit/test_estimate_upper_bound.py::test_m14_input_estimate_is_an_upper_bound_without_a_tokenizer`; `tests/unit/test_estimate_upper_bound.py::test_m14_chosen_text_stays_within_the_documented_bound`; `tests/unit/test_estimate_upper_bound.py::test_m14_heuristic_alone_bounds_identifiers_digits_and_symbols`; `tests/unit/test_estimate_upper_bound.py::test_m14_heuristic_never_exceeds_one_token_per_utf8_byte`; `tests/unit/test_estimate_upper_bound.py::test_m14_space_separated_digits_cost_a_token_each_and_per_space`; `tests/unit/test_estimate_upper_bound.py::test_m14_a_repeated_spaced_character_pays_for_every_space_it_cannot_ride`; `tests/unit/test_estimate_upper_bound.py::test_m14_mixed_line_endings_cost_a_token_per_change`; `tests/unit/test_estimate_upper_bound.py::test_m14_whitespace_that_changes_kind_costs_a_token_per_character`; `tests/unit/test_estimate_upper_bound.py::test_m14_tool_definitions_add_the_hidden_prompt_overhead`; `tests/unit/test_estimate_upper_bound.py::test_m14_long_context_tier_is_held_within_the_margin_of_the_threshold`; `tests/unit/test_estimate_upper_bound.py::test_m14_long_context_tier_is_held_when_the_byte_bound_reaches_the_threshold`; `tests/unit/test_estimate_upper_bound.py::test_m14_hold_covers_a_call_whose_real_count_crosses_the_long_context_threshold`; `tests/unit/test_estimate_upper_bound.py::test_m14_long_context_tier_is_never_missed_even_for_chosen_text`; `tests/unit/test_estimate_upper_bound.py::test_m14_tool_use_input_is_billed_as_json_and_the_long_tier_is_taken`; `tests/unit/test_estimate_upper_bound.py::test_m14_opaque_inputs_count_at_their_bytes`; `tests/unit/test_estimate_upper_bound.py::test_m14_structured_output_adds_its_hidden_prompt`; `tests/unit/test_estimate_upper_bound.py::test_m14_byte_bound_holds_each_image_at_the_catalog_image_ceiling` |
| M-15 | The upstream body carries exactly one output ceiling, the one the hold was sized with; a client's second ceiling field is never forwarded | `tests/integration/test_output_ceiling.py::test_m15_the_forwarded_ceiling_equals_the_held_one`; `tests/contract/providers/test_output_ceiling.py::test_m15_the_forwarded_ceiling_is_the_held_one_alone`; `tests/contract/providers/test_output_ceiling.py::test_m15_a_ceiling_endpoint_without_a_held_ceiling_is_refused`; `tests/unit/test_pipeline_steps.py::test_m15_the_clean_request_carries_one_ceiling_equal_to_the_held_one`; `tests/unit/test_estimate.py::test_m15_a_given_output_ceiling_is_held_as_is` |

## What is not guaranteed

- Provider-reported usage is billed as reported; absurd values are flagged, not capped. A provider
  that bills differently from its own usage numbers is outside what the guard can see.
- Prices come from the catalog. A wrong or stale price is wrong money; entries marked
  `verified: false` have not been compared with the provider's page ([add a model](catalog-how-to.md)).
- The input estimate is not a proven bound. It is measured against the o200k and cl100k
  tokenizers; Anthropic's and Google's are not public, so the 25% margin is what covers them.
  Natural and random text stays at or above the real count (1.04x at worst: random lowercase or
  rare-consonant words and random rare CJK or hangul use most of the margin). Text chosen to
  tokenize badly, such as `uo zyqo qxvmojzv` repeated, can reach about 1.6x the margined estimate;
  only one token per UTF-8 byte is a hard ceiling. Such a call overruns its hold; the overrun is
  booked in full and, with the default `strict_overrun`, blocks the budget until acknowledged. The
  long-context tier is chosen from the byte ceiling too, so a call is never held at the short tier
  and billed at the long one. Only the text part of that ceiling is proven (a byte-level tokenizer
  never spends more than one token per UTF-8 byte, and tool-call JSON and opaque inputs such as
  signatures and encrypted reasoning are counted at their bytes); images are assumed to cost at
  most the catalog image ceiling each and hidden provider prompts (tools, structured output, chat framing)
  to fit the documented overheads. Tool-call input is estimated from its compact JSON; if a
  provider renders it spaced or indented, digit-heavy input can reach about 1.6x the margined
  estimate (the byte bound still takes the long-context tier).
- Whoever controls the host can read keys and edit the ledger. Library mode is cooperative.
- The proxy may reach any internet host; there is no egress allowlist yet ([threat model](threat-model.md)).
