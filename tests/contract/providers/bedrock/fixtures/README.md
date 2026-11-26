# Bedrock fixtures recorded from the live API

Captured on 2026-11-26 in eu-west-1 through the `eu.anthropic.claude-haiku-5-5` inference profile, for the
spendgate 0.3.0 live smoke test (issue #9, `docs/reviews/live-smoke-v0.2.md`).

- `converse_text_live_20261126.json`, `converse_tool_live_20261126.json`: Converse answers, as the AWS CLI
  printed them (`_note` added; the tool-use id replaced with a fake of the same format).
- `converse_stream_live_20261126.bin`: the exact 880 response bytes of a ConverseStream call
  (`application/vnd.amazon.eventstream` frames with their CRCs), posted with a SigV4-signed request because
  AWS CLI v2 has no event-stream operations. No ids or account data are in it.
