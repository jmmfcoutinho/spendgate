## What and why

<!-- What changes, and the reason. Link the issue. -->

## Rule or target affected

<!-- Money rule, threat-model row or size target this touches. -->

## Tests

<!-- Tests added or changed, named for the rule they prove. -->

## Gate output

<!-- Paste `make check` result and the lightweight report delta
(`uv run python scripts/lightweight_report.py`). -->

## ADR

<!-- Link the ADR if public behaviour changed (config format, error codes, endpoints,
ledger schema, default policy, new runtime dependency, raised size limit). Otherwise "n/a". -->

## Checklist

- [ ] Docs updated
- [ ] CHANGELOG.md updated
- [ ] No new runtime dependency (or justified in an ADR)
- [ ] No secrets, real keys or personal data in the diff
