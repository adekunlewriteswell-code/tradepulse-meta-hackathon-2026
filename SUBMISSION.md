# TradePulse AI | Hackathon submission overview

## The problem
In FMCG retail distribution, a sales report can identify inactive outlets but often cannot explain **why** they stopped buying. TradePulse AI explores turning retailer-stated barriers into evidence-linked, human-reviewed activation plans.

## What the hackathon prototype does
1. **Listen.** A local browser simulates a numbered-menu feature-phone/USSD conversation. The completed session is stored with an inspectable trace.
2. **Understand.** A manager reads observations and an advisory, evidence-linked interpretation. The application has a Meta Model API adapter with structured-output validation, provenance and uncertainty tracking. Genuine Meta inference succeeded during local development; the public demonstration runs offline with explicitly labelled mock/simulated responses.
3. **Decide.** A human reviews the AI suggestion, drafts a bounded synthetic mission and separately approves activation. The AI cannot authorize a transaction or reward.
4. **Simulate and check.** A fictional retailer commits, then a deliberately simulated purchase enters an exact integer-kobo financial model. Deterministic guards enforce distinct-retailer eligibility, reward cap, positive contribution and margin.
5. **Review.** The dashboard presents the untouched fictional baseline, incremental synthetic scenario and arithmetic sum. It never claims observed sales or commercial uplift.

## Example demonstrated result
An invented retailer reports a shelf-space constraint. A saved advisory interpretation suggests discussing smaller orders or split delivery, subject to manager judgment. For a simulated ten-unit purchase, the UI calculates NGN 1,000 revenue, NGN 600 cost of goods, NGN 50 fulfilment, NGN 10 operating cost and a human-authorized NGN 50 simulated reward, leaving **NGN 290 contribution**. No real purchase, payment or retailer activation is verified.

## Technical implementation
Built during the event as a standalone Python FastAPI/SQLite app: a client-side browser simulator, manager intelligence UI, stored observations, Meta HTTP client adapter, validated structured output, idempotent request handling, audit event protections, human decision workflow, exact minor-unit finance, offline regression tests and security boundaries.

Run locally:
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.demo --mode simulated
```
Open http://127.0.0.1:8000/demo. No API key is required for offline demonstration. Run tests after stopping the server with ` .\.venv\Scripts\python.exe -m pytest tests -q `.

See README.md, docs/local_demo.md, docs/ai_integration.md and SYNTHETIC_DATA_NOTICE.md for technical details.

## Transparency and limitations
- **All browser retailer identities, observations, missions, purchases, reward releases and route economics are synthetic or simulated**, not field-verified business data.
- Numbered-menu interaction is browser-simulated; there is no live telecom USSD integration.
- No production deployment, authenticated commercial manager accounts, sale verification or real money transfer is supplied.
- Genuine Meta results were generated and saved locally during development; live credentials, provider responses and local databases are deliberately excluded from public source.
- A model's proposal is advisory and may be wrong. No controlled field evaluation or causal sales improvement is claimed.

**Theme:** Build for Your World.
