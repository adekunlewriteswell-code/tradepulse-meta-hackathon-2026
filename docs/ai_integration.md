# Milestone 1 AI boundary

Only `MockAIClient` is implemented. It performs no network I/O and needs no
credential. The model identifier is `mock-deterministic-v1`; the contract version
is `milestone-1-contract-v1`. These are local mock identifiers, not Meta models.
Setting `USE_MOCK_AI=false` raises an error rather than enabling a live client.

The async protocol is `extract_and_strategise(AnalysisRequest) -> str` (raw JSON).
Inputs contain a route ID and supplied observation IDs, retailer IDs, menu paths,
products, and stated barriers. The mock sorts observations and products for
determinism, copies supplied barriers, and emits one explicitly labelled manager
review recommendation. It does not infer commercial intent. The confidence
values are fixture constants, not calibrated probabilities.

The response contract covers barrier extraction and activation strategy
recommendations. It uses `intent_level` (singular), `pattern_summary`, uncertainty
flags, a nonempty uncertainty statement, and evidence references. A future live
prompt must match this validated contract.

Pydantic forbids unknown fields and implicit string-to-number coercion. Confidence
must be finite and in [0, 1]. Strategies require a nonempty evidence list;
the service cross-references both strategy and barrier evidence against the
supplied observation IDs. Empty/whitespace uncertainty is rejected.

`AIAnalysisService.analyze()` returns `AnalysisResult` with a validated output
or None, plus an **unsaved** `AIExtraction` provenance record. Callers persist
that record inside `Database.transaction()`; tests exercise successful and failed
record round trips. There is no production HTTP analysis endpoint yet.

| Outcome | Parse status | Validation status | Parsed output |
|---|---|---|---|
| Valid schema and supplied evidence references | PARSED | EXTRACTION_VALID | Validated response |
| Invalid JSON | FAILED | EXTRACTION_FAILED | None |
| Schema/evidence failure | PARSED | EXTRACTION_INVALID | None |
| AIUnavailableError | NOT_ATTEMPTED | UNEXTRACTED | None |

Raw request, raw response where available, model name, contract version,
observation IDs, error, statuses, source, and timestamp are stored by the model.
Failures never trigger fallback fabricated success. A future provider adapter
must translate expected provider failures into `AIUnavailableError`; programming
errors are not swallowed. The current module contains no provider implementation.

FastAPI injection is through `get_ai_client` and `get_ai_service`; tests cover
both factory injection and dependency overrides. The test-only probe route is
never part of the application's production router.

EXTRACTION_VALID means structure and supplied references passed validation.
It does not prove claims, evidence interpretation, purchase occurrence, real
Meta inference, or commercial correctness. AI cannot approve missions, verify
purchases, calculate/release rewards, or bypass manager approval. Real Meta
integration, authentication, the live prompt, token accounting, and UI status
display remain future work.
