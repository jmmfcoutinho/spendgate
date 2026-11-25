# ADR-0026: ElevenLabs speech accepts a validated `language_code`

**Status:** Accepted
**Date:** 2026-11-25

## Context

`POST /v1/audio/speech` takes the OpenAI speech shape (protocol.md section 2.6), and the ElevenLabs
adapter rebuilds its body from that shape alone: `model_id`, `text`, the voice id, the output
format and `voice_settings.speed`. ElevenLabs infers the language from the text, and for short or
mixed non-English text (Portuguese read-aloud, for example) it can pick the wrong one. Its
text-to-speech body has an optional, nullable `language_code` field (ISO 639-1) "used to enforce a
language for the model and text normalization"
([ElevenLabs API reference](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)).
The same reference says a model that does not support the given code ignores it, and that the
parameter is not supported for the `multilingual_v2` models. The OpenAI speech API has no such
field, so spendgate refused it as unknown (issue #10).

## Decision

- `POST /v1/audio/speech` accepts `language_code` **for ElevenLabs only**. It must be an ISO 639-1
  code: a string of exactly two lowercase ASCII letters (`pt`, `en`). Anything else (another case,
  a locale such as `pt-PT`, a three-letter code, a non-string) is `invalid_request` with
  `param: language_code`, before routing and again in `translate`; it is never forwarded or
  dropped silently.
- The field keeps ElevenLabs' own name and is forwarded unchanged in the JSON body. An absent or
  `null` value is not forwarded (ElevenLabs infers the language, as before).
- The shared OpenAI speech allowlist in `openai_shapes` is unchanged: `openai` and `azure_openai`
  still refuse `language_code` as an unsupported field rather than forwarding it to an API that
  does not document it.
- Only the code's shape is checked, not membership in the ISO 639-1 list or in a model's supported
  languages, and spendgate does not police which model gets the field. It has an effect only on
  models that support it, such as the v2.5 Turbo and Flash models; ElevenLabs ignores it otherwise,
  including on `eleven_multilingual_v2`.
- No other ElevenLabs voice setting (`seed`, `stability`, `similarity_boost`, ...) is added.

## Consequences

- Public behaviour: a speech request with a valid `language_code` for an ElevenLabs model is now
  served instead of refused. The same field sent to an OpenAI or Azure OpenAI speech model is
  still refused.
- Money is unaffected: ElevenLabs bills by characters, and `language_code` changes neither the
  hold nor the charge (the larger of the input length and the `character-cost` header), whether
  the model uses the code or ignores it.
- A client that sends `language_code` to a model that ignores it gets no error: the audio is
  generated as if the field were absent.
- OpenAI SDK clients send the field through `extra_body={"language_code": "pt"}`.
- Further ElevenLabs-only fields need their own decision; each one widens what reaches the
  provider.
