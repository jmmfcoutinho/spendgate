# Project decisions

Recorded 2026-11-13 by the maintainer. Defaults that were not contested are recorded as accepted and can be changed by a new ADR.

| # | Decision | Value | Reason | How decided | Date |
|---|---|---|---|---|---|
| D1 | Project and package name | **spendgate** | Says what it does (a gate on spend); free on PyPI and npm | Maintainer decision | 2026-11-13 |
| D2 | Licence | **Apache-2.0** | Permissive like MIT, plus an explicit patent grant that enterprises prefer | Recommended default, not contested | 2026-11-13 |
| D3 | Hosting | Maintainer's GitHub; open source from v0.1.0 | Open source from the first release | Recommended default, not contested | 2026-11-13 |
| D4 | Ledger currency | **Required**: every config names `ledger.currency`, with no default (library mode takes it from the cap). EUR was the original choice of default; the shipped schema requires the key instead | The maintainer and first users are in the EU; provider prices in USD are converted with a recorded FX rate | Recommended default (EUR), not contested; the released config requires the currency | 2026-11-13 |
| D5 | Telemetry | **None, ever** | Auditability and trust | Project principle | 2026-11-13 |
| D6 | v0.1 providers | OpenAI, Azure OpenAI, Anthropic, AWS Bedrock (Converse), Google (Gemini API and Vertex AI), Azure Speech, ElevenLabs | Covers the most used hosted LLM and speech APIs | Recommended default, not contested | 2026-11-13 |
| D7 | Python floor | **3.13** (CI also on 3.14) | Wide adoption | Recommended default, not contested | 2026-11-13 |
| D8 | Deployment scope for v0.1 | **Local only** (SDK, local proxy, Docker Compose); cloud patterns in [ROADMAP.md](../ROADMAP.md) | Keeps v0.1 small enough to audit | Maintainer decision | 2026-11-13 |

## Name check (2026-11-13; registers 2026-11-20)

- PyPI `spendgate`: free (404). npm `spendgate`: free (404).
- GitHub: 21 small repositories use the name, all with 0 stars. One, `buberlo/spendgate`, describes "EU Liability + Multi-Principal Policy for agentic commerce", an adjacent domain (agent payments); a possible source of confusion, not a blocker.
- Web search: no company or trademark found; a directory page ([serchen.com/company/spendgate](https://serchen.com/company/spendgate)) lists the name with no description.
- Trademark registers (2026-11-20): USPTO wordmark search for "spendgate": no results, live or dead. TMview (EUIPO, UK IPO and the national offices it covers), trade mark name contains "spendgate": no results, any class. PyPI `spendgate` still free.
