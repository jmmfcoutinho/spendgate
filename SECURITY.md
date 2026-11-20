# Security policy

- **Supported versions**: the latest minor release receives fixes; while 0.x, only the latest release.
- **Reporting**: privately through GitHub security advisories ("Report a vulnerability" on the Security tab). Do not open a public issue. Include version, configuration, reproduction and impact.
- **Response targets**: acknowledgement within 3 working days; initial assessment within 7 days; fix or mitigation for confirmed high/critical issues within 30 days; coordinated disclosure, with credit if wanted.
- **Scope**: bypass of a budget or limit, unpriced or unknown model reaching upstream, key leakage, auth bypass, perimeter escape, ledger corruption. Out of scope: attacks needing host or database access, denial of service by an authorised key within its limits.
- **Safe harbour**: good-faith research is welcome.
- **No telemetry**: spendgate sends nothing anywhere; reports reach maintainers only through the advisory channel.
- **Built in:** security is a gate on every change, not a final review: perimeter and block tests run in CI; `pip-audit`, detect-secrets and the image scan run on every PR and weekly on `main`.

Threats, controls and accepted risks: [docs/threat-model.md](docs/threat-model.md).
