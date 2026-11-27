# ADR-0031: A token-billed transcription holds the model's output limit

**Status:** Accepted
**Date:** 2026-11-27

## Context

Most transcription models are billed by the minute of audio. Some are billed per token instead:
gpt-4o-transcribe, gpt-4o-mini-transcribe and gpt-4o-transcribe-diarize on Azure OpenAI bill the
audio and prompt as input tokens and the transcript as output tokens. Their catalog entries carry
`output_per_mtok`.

The estimator only resolved an output ceiling for chat, responses, messages and token-billed
speech. For a transcription it returned 0, so the hold covered the audio and prompt but none of
the transcript, and every such call settled above its hold. That breaks the guarantee that
`spent + held` stays within the cap (M-03 books the excess as overrun; it does not prevent it),
and it contradicts M-09, which says a missing output ceiling is refused, never defaulted.

A transcription request has no field that bounds the transcript, so unlike chat the gateway cannot
forward a ceiling and make the provider stop at it. The only bound is the model's own output
limit.

## Decision

- **A transcription entry with an output meter** (`output_per_mtok`, `reasoning_per_mtok` or
  `audio_output_per_mtok`) **must set `limits.max_output_tokens`**, the model's documented output
  limit. The catalog schema and the loader reject such an entry without it, as they reject a chat
  entry without one.
- **The hold takes that value in full, whatever the key or the request sets.** A key's
  `max_output_tokens` (or `defaults.max_output_tokens`) cannot be sent upstream for a
  transcription, so letting it lower the hold would only shrink the hold, never the bill.
- **Defence in depth:** an entry that reaches the estimator without the limit (one that skipped
  validation) is refused with `model_not_priced`, never held at 0.
- The bundled token-billed Azure transcription entries get `max_output_tokens: 2000`, the limit
  OpenAI documents for the same models; each entry's note (or a comment above it) says so and
  that Microsoft's page was not checked for it. `scripts/azure_prices.py` writes it.
- Minute-billed transcription entries (Whisper, gpt-transcribe, Azure Speech) are unchanged: they
  have no output meter and hold no output.

## Consequences

- Holds for token-billed transcription grow by the output limit at the dearest output meter:
  2,000 tokens is at most 0.0264 USD per call on the bundled entries. They also count against the
  key's `tpm`.
- A custom catalog with a transcription entry that has an output meter and no
  `limits.max_output_tokens` no longer loads; add the model's documented output limit. A
  `spendgate catalog import` from LiteLLM skips such a model and says why.
- Token-billed speech (an entry with `audio_output_per_mtok`; none is bundled) still lets a key cap
  lower its hold although no request field bounds the audio either. It is left for a separate
  change.
- Not guaranteed: the limit is the provider's documented one. A provider that bills more than it
  documents is booked in full as overrun (M-03).
