# ADR-0025: Holds cover what the provider can bill, and usage is trusted only when complete

**Status:** Accepted
**Date:** 2026-11-22

## Context

A review of v0.1.1 found several ways a call could be held for far less than the provider then
billed, or settled below what was billed. Each one breaks the guarantee in
[how-money-is-protected.md](../how-money-is-protected.md) that `spent + held` stays within the cap
and that a call is charged the full hold unless usage was seen:

- inline media of any type was accepted in image positions and held as one small image;
- image-shaped objects outside content positions were held as one image instead of as text;
- the per-image hold was below what current models bill for one large image;
- the input-token heuristic undercounted text that tokenizers split finely, and the long-context
  price tier was chosen from that estimate alone;
- tool-call input and opaque reasoning inputs were partly or not counted;
- a request naming two output-ceiling fields was held at the smaller and forwarded with both;
- stream relays reported running totals from mid-stream chunks as final usage;
- usage missing a required counter, or in another endpoint's shape, was read as zero.

## Decision

- **Inputs the estimate cannot price are refused.** Inline media must be a base64 png, jpeg, webp or
  gif image (per adapter subset); other inline media is `invalid_request` before routing.
- **Images** are held at the catalog entry's `limits.max_image_tokens`, or at a default of 61,000
  tokens (the largest documented per-image cost plus 25%). Entries may only lower it, with a cited
  source. An object counts as an image only in a content position.
- **Text** is estimated by a heuristic (`core/heuristic.py`) that is at or above o200k and cl100k
  for natural and random text after the 25% margin. Deliberately chosen text can still tokenize up
  to about 1.6x the margined estimate; no tokenizer exceeds one token per UTF-8 byte.
- **The long-context tier is never missed for text:** it is taken when the margined estimate reaches
  80% of the threshold (`input_tokens x 1.25 >= threshold`), or when a byte bound (UTF-8 bytes of
  all counted text, tool JSON rendered with indentation, opaque inputs, overheads and the image
  ceilings) reaches the threshold.
- **Hidden prompts** for tools and structured output get documented fixed overheads.
- **One output ceiling** is resolved once, held, and forwarded alone (M-15).
- **Usage is trusted only when final and complete:** stream relays report end-of-stream usage only
  (M-13); a missing required counter, a null cost-raising counter, another endpoint's shape, or a
  total that contradicts its parts makes usage unreadable, so the full hold is charged.

## Consequences

- Holds are larger: about 20-40% more input for English prose and code, up to about 73% for
  accented Latin-script text, and much more for images (they also count against per-key `tpm`).
  Long-context tiers are held earlier (from roughly 20% of the threshold for prose).
- Some requests that were accepted are now refused (non-image inline media, `data:image/jpg` on
  Bedrock, multi-line base64, non-string Responses `instructions`, unknown image `detail` values).
- An OpenAI-compatible upstream must honour `max_completion_tokens` and send the usage-only stream
  chunk, or the ceiling is not enforced upstream and streams are charged the full hold.
- Not guaranteed: chosen text can exceed the input estimate by up to about 1.6x (booked as overrun,
  blocking under `strict_overrun`); images are assumed to cost at most the catalog ceiling; hidden
  provider prompts are assumed to fit the documented overheads.
