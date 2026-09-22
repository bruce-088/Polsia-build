# Polsia — AI Business Agent Platform

An autonomous multi-agent system that runs your business on autopilot. Nine specialized AI agents handle social media, competitor research, email outreach, ads management, customer support, code generation, business planning, finance, and orchestration — all powered by Claude Code CLI in headless mode.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                         nginx                           │
│              / → frontend   /api/ → backend             │
└────────────────┬────────────────────┬───────────────────┘
                 │                    │
        ┌────────▼──────┐    ┌────────▼──────────┐
        │  Next.js 14   │    │  FastAPI (Python)  │
        │  (frontend)   │    │  + WebSocket       │
        └───────────────┘    └────────┬───────────┘
                                      │
              ┌───────────────────────┼───────────────────┐
              │                       │                   │
     ┌────────▼────────┐   ┌──────────▼──────┐  ┌────────▼──────┐
     │  PostgreSQL 16  │   │  Redis + Celery  │  │  ChromaDB     │
     │  (15 tables)    │   │  (task queue)    │  │  (vector mem) │
     └─────────────────┘   └──────────────────┘  └───────────────┘
```

### The 9 Agents

| Agent | Responsibility | Schedule |
|-------|---------------|----------|
| **Orchestrator** | Writes morning plan + evening summary | 06:00 / 20:00 |
| **Business Planning** | Strategy, KPIs, growth recommendations | Daily |
| **Competitor Research** | Web search, profile updates | Daily |
| **Social Media** | Draft + post tweets | Every 2h |
| **Email Outreach** | Prospect finding + cold email sequences | Every 3h |
| **Customer Support** | Read inbox, draft replies | Every 3h |
| **Ads Management** | Google + Meta campaign optimization | Every 6h |
| **Code Generation** | Ship features, open PRs | On demand |
| **Finance** | Stripe revenue sync, spend tracking | Every 6h |

Each agent calls `claude -p "..." --output-format json` as a subprocess. No API key is needed — the Claude Code CLI authenticates via OAuth stored at `~/.claude`.

## Prerequisites

- Docker + Docker Compose
- [Claude Code CLI](https://docs.anthropic.com/claude-code) installed and authenticated (`claude login`)
- (Optional for real operations) Stripe, Twitter, SendGrid, Tavily, Google Ads, Meta Ads accounts

## Quick Start

```bash
# 1. Clone and configure
git clone <repo>
cd polsia
cp .env.example .env
# Edit .env — at minimum set API_KEY and SANDBOX_MODE=true

# 2. Start all services
docker-compose up -d

# 3. Initialize the database and seed company data
make init-db

# 4. Open the dashboard
open http://localhost
```

The stack is up when `http://localhost/api/v1/health` returns `{"status": "ok"}`.

## Configuration

### Essential settings (`.env`)

| Variable | Description |
|----------|-------------|
| `API_KEY` | Secret key for the dashboard API (`X-API-Key` header) |
| `SANDBOX_MODE` | `true` = no real posts/emails/charges (default) |
| `CLAUDE_CLI_PATH` | Path to the `claude` binary (default `/usr/local/bin/claude`) |

The Claude CLI authenticates via the `~/.claude` directory on your host, which is mounted read-only into the containers. Run `claude login` on your host once before starting the stack.

### Integrations

The following integrations are live and working today (fill in the relevant `.env` keys to activate each one):

- **Stripe** — read-only polling of revenue and account balance
- **SendGrid** — sends email, only when an agent explicitly decides to send
- **Twitter/X** — posts content, only when an agent explicitly decides to post
- **Tavily** — grounded web search used for real prospect research
- **IMAP** — detects real inbound email replies; a narrow auto-send gate allows only simple replies to go out automatically
- **GitHub** — powers the PR mechanism used by the code generation agent, limited to single-file changes

Google Ads and Meta Ads are configured but not yet wired to live actions.

## Development

### Make commands

```bash
make up              # Start all Docker services
make down            # Stop all services
make build           # Rebuild images
make logs            # Tail all logs
make shell-backend   # Open a shell in the backend container
make migrate         # Run Alembic migrations
make seed            # Seed company data
make init-db         # migrate + seed
make test            # Run unit + integration tests
make test-unit       # Unit tests only
make lint            # Ruff + mypy + ESLint
make reset-db        # Drop and recreate schema
```

### Running tests locally (no Claude credentials needed)

```bash
cd backend
CLAUDE_CLI_MOCK=true pytest tests/unit/ -v --cov=app
```

The `CLAUDE_CLI_MOCK=true` flag makes all agents return a mock response without invoking the CLI binary. This is also how CI works.

### Project layout

```
polsia/
├── backend/
│   ├── app/
│   │   ├── agents/          # 9 agent implementations
│   │   │   ├── base_agent.py        # call_claude() subprocess wrapper
│   │   │   ├── crew_factory.py      # AGENT_MAP registry
│   │   │   ├── orchestrator/
│   │   │   ├── social_media/
│   │   │   ├── competitor_research/
│   │   │   ├── business_planning/
│   │   │   ├── email_outreach/
│   │   │   ├── customer_support/
│   │   │   ├── ads_management/
│   │   │   ├── code_generation/
│   │   │   └── finance/
│   │   ├── api/v1/          # REST endpoints + WebSocket
│   │   ├── core/            # DB, Redis, ChromaDB, security, retry
│   │   ├── models/          # SQLAlchemy ORM (15 tables)
│   │   ├── schemas/         # Pydantic request/response models
│   │   └── services/        # Business logic layer
│   ├── celery_app/          # Celery worker, Beat schedule, tasks
│   ├── alembic/             # Database migrations
│   └── tests/
│       ├── unit/            # SQLite in-memory, CLAUDE_CLI_MOCK=true
│       └── integration/     # Testcontainers (real Postgres + Redis)
├── frontend/
│   └── src/
│       ├── app/             # Next.js 14 pages (9 routes)
│       ├── components/      # ActivityFeed, MetricsCard, AgentStatusGrid, Sidebar
│       ├── hooks/           # useActivityFeed (WebSocket), useAgentStatus (polling)
│       └── lib/             # Typed API client
├── e2e/                     # Playwright end-to-end tests
├── nginx/                   # Reverse proxy config
├── scripts/                 # init_db.sh, seed_company.py
├── docker-compose.yml
├── docker-compose.ci.yml    # CI override (CLAUDE_CLI_MOCK=true, no nginx)
└── Makefile
```

## CI/CD

Three GitHub Actions workflows run automatically:

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `ci.yml` | PR + push to main | Lint (ruff, mypy, ESLint), unit tests |
| `integration.yml` | Push to main | Integration tests (testcontainers), Playwright E2E |
| `docker-build.yml` | Push to main | Build all images, health-check the stack |

All CI jobs set `CLAUDE_CLI_MOCK=true` — no Claude credentials are needed in GitHub Actions.

## Turning off sandbox mode

When you're ready to let agents operate for real:

1. Set `SANDBOX_MODE=false` in `.env`
2. Fill in the relevant API keys for the integrations you want active
3. Restart the stack: `make down && make up`

Agents will now post to Twitter, send emails, create ad campaigns, and issue Stripe operations.


## Company OS Stage 1 decision mode

Polsia supports an opt-in, strict decision-envelope mode for synthetic Company OS evaluation.

Set task metadata:

```json
{
  "response_contract": "company_os_stage1",
  "scenario_id": "SIM-001"
}
```

In this mode the selected agent must return one clean JSON object containing:

- `scenario_id`
- `action`
- `risk_level`
- `state`
- `founder_approval`
- `handoff_to`
- `actions_taken`
- `actions_proposed`
- `assumptions`
- optional `notes`

Malformed, wrapped, incomplete, mismatched, or extra-field output raises a contract error. The runtime does not translate agent-specific prose, fill missing governance fields, or silently fall back to a summary.

Normal agent behavior is unchanged when `response_contract` is absent. This mode performs no policy decision or external action by itself; those are separate remediation layers.

### Central policy gate

`app.agents.company_os_policy` evaluates structured action intent before an
execution boundary. It enforces RED approval, hard and non-approvable blocks,
full-request limits (including split-limit evasion), integration write modes,
and explicit authority or consent. The gate is fail-closed and pure: it does
not execute actions or alter an agent's native decision envelope.

### Workflow-state validation

`app.agents.company_os_workflow` validates canonical workflow definitions and
requires an exact registered `(current state, action, proposed state)`
transition. It rejects unknown states, nonexistent transitions, risk mismatch,
ambiguous definitions, and movement from terminal states. The validator
returns canonical requirements and integration metadata but never repairs an
agent proposal or executes the transition.

### Integration-state enforcement

`app.agents.company_os_integration` treats `draft`, `propose`, `read`, and
`execute` as separate capabilities. Execution requires a registered intended
use, authorized scope, verified integration, a write-capable mode, and an
explicit external-write capability. Credentials or an `external_writes` flag
cannot override `disabled`, `documented`, or `read_only` mode. The service
authorizes capability only; it contains no connector or execution code.
