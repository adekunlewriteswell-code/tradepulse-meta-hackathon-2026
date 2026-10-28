# TradePulse AI — Milestone 1

TradePulse AI explores evidence-based activation of inactive FMCG retailers.
This original implementation for the Meta Global AI Developer Hackathon 2026,
started October 27, provides a local FastAPI foundation, synthetic SQLite data,
and an injectable deterministic mock AI client. It does not yet implement the
retailer workflow, user interface, reward engine, or real Meta inference.

All competition source was written for this entry. It is independent of existing
commercial products; no commercial code, data, schemas, or credentials are included.

**SYNTHETIC DEMONSTRATION DATA — not verified commercial transactions.**
See [SYNTHETIC_DATA_NOTICE.md](SYNTHETIC_DATA_NOTICE.md).

## Project structure

```text
app/routers/          HTTP routes
app/services/         AI analysis and transactional synthetic seeding
app/domain/           Exact money conversion and UTC timestamp validation
app/models/db/        SQLAlchemy models and database constraints
app/models/schemas/   Pydantic request, response, and metadata contracts
app/ai/               AI client protocol and deterministic mock
app/config.py         Environment settings and local database restrictions
app/database.py       Async database lifecycle, transactions, and audit guards
data/                 Synthetic seeder entry points; no database is distributed
tests/                Offline health, persistence, validation, and regression tests
docs/ai_integration.md Mock AI contract and provenance boundaries
```

## Runtime and installation

Verified runtime: **CPython 3.13.5, Windows x64, SQLite 3.49.1**.
`pyproject.toml` declares Python >=3.11; Python 3.11, 3.12, and other platforms
have not been tested. Use the verified interpreter for this milestone.

Extract or check out the source into its own directory. Run these PowerShell
commands from the directory containing this README, using Python 3.13.5:

```powershell
python --version
python -m venv .venv
New-Item -ItemType Directory -Force .tmp | Out-Null
$env:TEMP = Join-Path $PWD '.tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $PWD '.pip-cache'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m data.seed_synthetic
.\.venv\Scripts\python.exe -m app.main
```

The server listens on `http://127.0.0.1:8000`; stop it with Ctrl+C.
`GET /health` returns HTTP 200 and exactly `{"status":"ok"}`.
`GET /` returns service metadata; OpenAPI documentation is at `/docs`.
The health endpoint is a liveness check; it does not run a database query.
Schema initialization must succeed before application startup completes.

Activation of the virtual environment is optional: every command above names
its interpreter explicitly. No global packages are installed.
`requirements.in` records direct pins; `requirements.txt` pins the full resolved
set, including the local pip installer. The pinned stack uses FastAPI 0.162.0,
Starlette 1.10.0, and its supported HTTPX2 test-client backend. Tests treat all
warnings as errors. FastAPI's required OpenTelemetry API package
is installed; no telemetry exporter or external telemetry configuration is added.
Pinning alone is not a dependency security audit or a guarantee against vulnerabilities.

## Configuration

No `.env` or credentials are required. Defaults match `.env.example`; optionally
copy that example to `.env`, which is ignored. Environment variables override
the file, which is read only from the workspace root.

| Variable | Default | Behavior |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/tradepulse.db` | File-backed SQLite database, restricted to this workspace; URI options and other database drivers rejected |
| `USE_MOCK_AI` | `true` | Must be true; false causes a clear factory error, without contacting a provider |

There is no real Meta client, API key configuration, billing configuration,
deployment configuration, or outgoing model call in this milestone.

## Seed and persistence

Both entry points work:

```powershell
.\.venv\Scripts\python.exe -m data.seed_synthetic
.\.venv\Scripts\python.exe data/seed_synthetic.py
```

On a fresh database, seeding creates 10 retailers (2 ACTIVE, 8 INACTIVE) and one
route baseline. Every row is labelled `data_source="synthetic"`, with invented
names, stable IDs, and fixed timestamps. Baseline money and period are fixed.
Seeding is transactional and insert-only: repeats leave all row values unchanged,
restore missing fixture rows, and preserve subsequent status changes. It is not
a demo reset command. Conflicting reserved IDs with different provenance are
rejected instead of relabelled. CLI counts reflect the actual current data.

SQLite uses WAL, foreign keys, recursive triggers, and a 5-second busy timeout on
each application connection. SQLAlchemy explicitly controls BEGIN, including
transactional DDL. This follows the documented
[SQLAlchemy SQLite transaction pattern](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#enabling-non-legacy-sqlite-transactional-modes-with-the-sqlite3-or-aiosqlite-driver).
The application lifespan disposes its engine; services use transaction contexts
that commit on success and roll back on exceptions.

Tables: `retailers`, `route_baselines`, `observations`, `missions`, `commitments`,
`purchase_events`, `reward_releases`, `audit_logs`, and `ai_extractions`.
`create_schema()` creates a fresh schema and idempotently installs triggers.
It also performs one targeted transactional upgrade of the original audit table
to `WITHOUT ROWID`, preserving every declared column and record ID. It refuses
detected custom audit schemas or incoming foreign keys rather than discarding
them. Hidden rowids are intentionally removed. This is not a general migration
framework. Existing route/mission tables receive compatibility guards without
being rebuilt; invalid historical balances or mission metadata stop startup for
explicit review. They are not silently rewritten. Run this upgrade with the
local server stopped; keep a database backup before changing an existing schema.

## Exact monetary representation

All monetary columns use **signed 64-bit SQLite INTEGER minor units**, with a
fixed two-decimal scale. GBP is the invented demonstration baseline currency:
1999 minor units means GBP 19.99. Costs, prices, rewards, and snapshots never
use SQLite REAL or SQLAlchemy Numeric. Nonnegative money fields have database
type/range checks; contribution may be negative. Monetary conversion accepts
Decimal or decimal strings and rejects floats, nonfinite values, fractional
minor units, and overflow. Conversion uses exact Decimal tuples independently
of ambient Decimal precision/exponent settings. Negative contribution values
require explicit `signed=True` on the conversion helpers; price/cost/reward
columns remain nonnegative. The baseline's guarded SQL CASE checks each
subtraction's bounds before evaluating it, preventing overflow promotion to
REAL. It never silently rounds. Financial calculations use Decimal; no reward
calculation is implemented yet.

Rates use exact integer basis points (`500` = 5%); quantities use integers.
Audit details and profitability snapshots reject JSON real numbers in SQLite
triggers. Use integer `*_minor` / `*_bps` values in these JSON objects. AI
confidence scores may use floats; they are not money. SQLite's distinction
between exact integers and approximate reals is documented
[here](https://www.sqlite.org/floatingpoint.html).

The synthetic monthly baseline is GBP 2,000 revenue minus GBP 1,400 COGS,
GBP 100 fulfillment, GBP 0 incentives, and GBP 50 other costs = GBP 450
contribution. This is an arbitrary route baseline for the two existing active
retailers; the eight inactive retailers have no seeded purchases. It is not an
estimate of real performance. Baseline comparison and mission profitability
evaluation belong to later milestones.

GBP is the current fixture currency. A switch to NGN, with an explicitly chosen
synthetic amount set, remains a product decision. No currency labels or amounts
have been converted or relabelled.

## Integrity and audit boundaries

Database constraints enforce unique purchase idempotency keys, unique
mission/retailer/order references, one commitment per retailer/mission, and one
release per mission with a unique release key. Purchase keys follow
`{mission_id}:{retailer_id}:{order_ref}`; release keys follow `{mission_id}:release`.
Composite foreign keys preserve route and participant associations.
Purchases require `simulated_purchase=true` and `data_source="simulated"`;
reward releases also require simulated provenance.

The audit table has no hidden `rowid`, `_rowid_`, or `oid`. SQLite triggers reject
UPDATE, DELETE, and primary-key replacement, including through independent
connections with recursive triggers disabled. These guards are not
tamper-proof against the database file owner: privileged access can drop
triggers/tables or replace the file. There is no cryptographic audit chain or
external immutable store. Direct writers must separately enable SQLite foreign
keys; this application enables them on every connection it creates.

Mission ORM writes use `MissionMetadata` to validate timezone-aware inputs,
normalize them to fixed-width microsecond UTC strings, enforce end > start,
and reject blank approval fields. Direct SQL must already use that canonical
representation; database checks/triggers reject other offsets, invalid dates,
and incomplete or whitespace-only required metadata. This validates metadata,
not manager authentication or permission to approve a mission.

Only read-only HTTP routes exist. Mission approval, manager authentication,
state transition enforcement, returning existing rows on duplicate HTTP
requests, reward eligibility, and atomic reward-release-plus-audit orchestration
remain deferred. No purchase/reward write endpoint is exposed without those
future gates. Database constraints are the foundation for those services.

## Mock AI and provenance

See [docs/ai_integration.md](docs/ai_integration.md). `AIClient` is an async
protocol; `MockAIClient` supplies explicit mock results. `create_app()` supports
client injection, and FastAPI's `get_ai_client` dependency can be overridden.
No AI HTTP endpoint is exposed in Milestone 1; tests attach a test-only route
to verify actual FastAPI dependency resolution.

## Tests

Stop the server with Ctrl+C, then run from the source directory using the same
virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

Tests require no external services or credentials. HTTPX and HTTPX2 network transports
are blocked by the test fixtures; in-process ASGI requests remain available.
Both seed commands also run in child Python processes against a local test DB.
Temporary databases live under `.test-artifacts/pytest` inside this workspace.
Pytest replaces that disposable directory on each run; do not store work in it.

The verified Windows CPython 3.13.5 environment passed 167 tests on October 28,
2026. This is a recorded result, not a guarantee for other environments. The
command above reproduces the suite without credentials or external services.

## Publication status

This source package contains the Milestone 1 foundation, tests, and public
technical documentation. It excludes internal planning, engineering reports,
local databases, virtual environments, caches, credentials, and test artifacts.
No license has been selected; licensing and final organizer requirements remain
pending. Package preparation does not constitute deployment or completion of
the competition's real-Meta-inference requirement.
