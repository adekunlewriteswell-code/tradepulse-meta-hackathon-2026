# Meta integration and stored evidence

| Mode | Client | Provenance source | Genuine inference |
|---|---|---|---|
| Mock | Deterministic MockAIClient | mock | No |
| Simulated | Real MetaAIClient with HTTPX MockTransport | meta_simulated | No |
| Live | Real MetaAIClient with HTTPS and server-side key | meta_live | Only after separate authorization |

Live access is disabled by default. Provider failure never becomes mock success.
The default HTTP API remains read-only. The explicitly enabled localhost browser demo
adds protected offline analysis, reusing the same stored-observation service. See
[local_demo.md](local_demo.md) for its boundaries; no live browser mode is exposed.

## Trusted persisted observations

The fictional NGN route has ten synthetic retailers, two active and eight inactive.
The observation fixture persists eight inactive-retailer observations covering minimum
orders, delivery reliability, working capital, availability and low sales velocity.

Observation retains its existing columns. raw_session_log uses a strict
synthetic-observation-v1 JSON object with schema_version, data_source, menu_path,
products and stated_barrier. Both raw inputs and records are explicitly synthetic.
Legacy raw arrays remain stored but this workflow rejects them without reinterpretation.

The loader takes IDs, not caller-supplied observation records. It checks persisted
route/retailer ownership, NGN and synthetic provenance, unique IDs, 1-8 observations,
4,096-byte raw-session bounds, field lengths and matching numbered menu paths.
Invalid input fails before inference. Observations are not purchases or proven demand.

## Provider request and validation

AIClient remains async extract_and_strategise(AnalysisRequest) -> str.
MetaAIClient uses HTTPX and a fixed HTTPS destination. It validates settings again,
disables redirects/environment proxies and sends credentials only in the auth header.

The configured model defaults to muse-spark-1.3. Developer instructions separate untrusted
retailer statements from instructions; the user data is JSON encoded. The request uses
response_format.json_schema with strict=true and a schema derived from AnalysisResponse.
max_completion_tokens bounds visible/reasoning output. Prompt version is
meta-barriers-strategies-v2; provider contract version is analysis-contract-v2.
Evidence fields are constrained to enums of the supplied observation IDs; the developer
instructions require bare IDs without appended explanations. Independent validation still
requires exact evidence IDs and exact copying of the associated retailer statements.

Envelope checks reject model mismatch, refusals, incomplete generations, tools, malformed
usage and oversized/invalid responses. Independent strict Pydantic validation checks
confidence, nonempty uncertainty, supplied observation IDs/products and exact copying of
cited stated barriers. Inferred intent and strategies remain advisory.

Prompt-injection containment is not proof of model obedience. The analysis has no execution
path to approve missions, verify purchases, calculate profitability or release rewards.
Structural/reference validation does not prove commercial correctness.

Sources: [API reference](https://dev.meta.ai/docs/api-reference),
[request schema](https://dev.meta.ai/docs/api-reference/chat-completions/schemas),
[structured output](https://dev.meta.ai/docs/structured-output),
[rate limits](https://dev.meta.ai/docs/pricing-rate-limits).

## Transactions, provenance and replay

StoredAnalysisService loads evidence, then atomically claims a unique analysis_key.
Its fingerprint binds evidence content, route, model, mode and prompt/observation versions.
Concurrent duplicates do not issue another provider call. Completed outcomes, including
failures, replay without inference. Reusing a key for different inputs/modes is a conflict.

No database transaction remains open during the remote await. A second transaction saves
the result. A completion-write failure, cancellation or process loss can leave PENDING.
Manual reconciliation is required before issuing a new potentially billable request;
there is no automatic lease expiry or retry after ambiguous completion.

AIExtraction gains four nullable fields: analysis_key, request_fingerprint, run_state and
call_details, with a unique analysis-key index. A targeted transactional ALTER upgrade
preserves old rows, leaving their new fields NULL. Incompatible custom fields/indexes fail
for review. All nine existing tables and the audit protections remain.

Provenance records IDs, model/source, versions, safe request/response details, per-attempt
status/request IDs, available usage, parsed output, validation and safe errors.
The configured key and bearer tokens are redacted from persisted content. Authorization
headers are never persisted. The CLI does not print provider exception strings or bodies.

The lower-level AIAnalysisService still returns an unsaved provenance record; the stored
workflow supplies durable claim/result handling.

## Reliability limits

Default adapter bounds: 5s connect, 20s read, 30s total timeout, one concurrent call,
16,384 request bytes, 65,536 decoded response bytes, 2,048 output tokens, three attempted
calls and 60,000 reserved tokens. HTTPX also bounds write/pool waits; total time includes
queueing/retry waits.

A pessimistic token reservation uses full encoded request bytes plus maximum output tokens.
This is a conservative proxy, not a verified provider tokenizer or a monetary cap.
Failures retain reservations. Reported usage exceeding its bound blocks further calls.

Retries default to zero. At most one configured retry is allowed, only on a 429 rejection
with numeric Retry-After between zero and two seconds. Longer/date-based waits fail for
manual review. 400/401/403, 5xx, redirects, timeouts and transport errors are not retried.
Every attempt consumes budget.

Budgets/concurrency are per adapter/process. A restart/new adapter resets them; they are
not a cross-process account quota. Same-key database replay survives restarts. Provider
hard spending limits, account access and actual billing still need separate verification.

Owned application clients close during lifespan cleanup, including DB cleanup failures.
Injected clients remain caller-owned. The CLI closes its DB/client in finally blocks.

## Offline demonstration

Run from the project root after installing the pinned requirements in a local venv:

```powershell
.\.venv\Scripts\python.exe -m data.seed_synthetic
.\.venv\Scripts\python.exe -m data.analyze_observations --mode mock --seed-observations --analysis-key demo-mock
.\.venv\Scripts\python.exe -m data.analyze_observations --mode simulated --analysis-key demo-simulated
```

Replay the same key to return the saved outcome. The CLI prints mode, validation,
NGN baseline, recommendation evidence and uncertainty. Default CLI bounds are one attempted
call, 12,000 reserved tokens and 1,024 output tokens. Simulation uses a non-provider fixture
credential; it does not use a real API key.

Live CLI execution requires --mode live, --live-authorized, META_LIVE_ENABLED=true and a
server-side MODEL_API_KEY. The application factory additionally uses USE_MOCK_AI.
Documentation and passing offline tests do not authorize a live call. Real inference
quality and account/model/billing availability remain unverified.
