# Local retailer and manager demonstration

All activity is synthetic. An observation is not a purchase, a recommendation is not a
verified fact, and accepting it for planning is not mission or financial authorization.

From the project root, using the existing pinned local environment:

```powershell
.\.venv\Scripts\python.exe -m app.demo --mode simulated
```

Open `http://127.0.0.1:8000/demo`. `--mode mock` changes the initial offline selector;
both modes remain available in the manager screen. The launcher binds only 127.0.0.1,
disables proxy-header trust and access logging, clears the provider key from its settings,
and forces offline mode. It never invokes genuine Meta inference. Normal `app.main`
startup keeps the demo disabled unless DEMO_ENABLED is explicitly enabled.

## A three-minute local journey

1. Choose an invented retailer and start the simulated dial session. Use numbered keys
   to select interest, product and barrier, then confirm. Back returns to the previous
   menu; 0 or Cancel ends without an observation. Restart creates a new session while
   retaining the previous session's saved history. Invalid options show a retry message.
2. Open Manager intelligence. Select the saved observation in the evidence inbox.
   Inspect its raw session and full numbered-menu trace. The original 2-active/8-inactive
   route baseline remains unchanged. It does not grow when someone submits interest.
3. Analyze in explicit Mock or Simulated mode. Inspect recommendation citations,
   confidence, preconditions, risks and uncertainty. A simulated HTTP response exercises
   the real adapter, but is labelled offline and is not actual Meta output.
4. Mark a strategy reviewed, accepted for further planning or rejected. This appends a
   labelled local demo review event. No typed manager name is used as authentication.

## State and persistence

HOME -> PRODUCT -> BARRIER -> CONFIRM -> COMPLETED. CANCELLED and EXPIRED are terminal.
Sessions expire after 30 minutes and accept at most 40 actions. No free-form retailer
text is accepted; product/barrier statements are mapped from fixed numbered options.
Back clears later selections. Only confirmation persists an Observation using the
existing synthetic-observation-v1 raw-session contract.

Server state snapshots, operation receipts, browser capability digests and review
decisions are explicitly labelled DEMO_* append-only AuditLog events. The existing nine
tables, constraints and audit triggers are retained; no schema rebuild is needed.
The raw Observation log keeps the canonical menu path and selected facts. The separate
append-only session trace also preserves Back, Cancel and invalid numbered inputs.

Every operation has a browser-bound request UUID. A first-statement idempotent insert
serializes writes before reading state, avoiding SQLite read-to-write upgrade races.
Version checks reject stale actions; duplicate request keys replay without another
registration. A deterministic observation primary key enforces one completion per demo
session. Failed writes roll back the operation receipt, state and observation together.

The browser saves only session IDs and pending operation payloads locally. Server state
is authoritative; refresh resumes it, and failures retain the same request UUID for
explicit retry. Cookies/capability records survive server restarts for 24 hours. Clearing
the cookie, expiring it or changing host prevents session ownership recovery; saved
observations remain visible to a local manager. Browser-storage failure does not undo
server persistence but prevents automatic selection recovery after refresh.

## Security and scope

- DEMO_ENABLED defaults false. Demo routes are omitted entirely when disabled.
- Direct HTTP loopback peer and Host are required; forwarded headers, cross-origin
  requests and cross-site browser requests are rejected. No CORS permission is added.
- State changes require same-origin JSON POST, an existing HttpOnly SameSite=Strict
  browser capability cookie, and its derived CSRF token. The capability is only a local
  demonstration boundary, not real retailer or manager authentication.
- Raw capability values are not stored in SQLite or returned in application JSON.
  Only their digests are saved. The browser obtains a derived CSRF token from bootstrap.
- Requests are capped at 8,192 bytes, duplicate-field/nonfinite JSON is rejected, and
  strict schema lengths, routes, owners and state versions are validated server-side.
- Browser text uses DOM textContent, never interpolated HTML. CSP permits only local
  scripts/styles and forbids framing, external connections, inline script and objects.
- The browser receives only safe analysis fields, not provider credentials, authorization
  headers or raw provider provenance. The existing real adapter remains available through
  its separately authorized CLI path and cannot be selected by this browser workflow.
- AI inference still runs outside database transactions, through StoredAnalysisService.
  Completed failure replay does not rerun inference. Pending claims require reconciliation.
- All reviews are demo-only records with not_mission_approval=true. No business mission,
  commitment, purchase or reward is created by this workflow.

This is not an authenticated internet-facing management system. Use one local process;
do not bind to external interfaces, reverse proxy it, tunnel it or deploy it publicly.
Mock/simulated limits and per-process budgets remain as documented in ai_integration.md.

## Demo endpoints

All are under `/demo`; static UI is `/demo` and `/demo/assets/{demo.css,demo.js}`.
Bootstrap establishes the local cookie and returns the derived CSRF token. All other
API calls require a recognized browser capability; every POST also checks Origin/CSRF.

| Method | Path after /demo/api | Purpose |
|---|---|---|
| GET | /bootstrap | Establish local browser capability |
| GET | /overview | Route, retailers, newest 100 observations, newest 20 offline analyses |
| POST | /sessions | Begin or replay a browser-bound session |
| GET | /sessions/{uuid} | Retrieve owned server state |
| POST | /sessions/{uuid}/input | Versioned digit, Back or Cancel action |
| POST | /analyses | Select 1-8 unique stored observations; mock or simulated only |
| GET | /analyses/{id} | Validated offline result, safe status and local review history |
| GET | /evidence/{id}/trace | Inspect persisted numbered-menu interaction log |
| POST | /reviews | Append an explicitly acknowledged demo-only decision |

Demo endpoints are excluded from the default OpenAPI surface. Browser writes require
X-Demo-CSRF, with the token from bootstrap, and exact matching Origin. Menu inputs are
single ASCII digits. Review decisions are reviewed, planning or rejected, with demo_only
set to true. No production mission/reward endpoints are implemented here.
