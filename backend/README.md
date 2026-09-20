# AI Trading Bot Research Society Backend

Backend Foundation v0.1 built with FastAPI.

## Features

- Health check
- Agent registry
- Mission creation
- Mission listing
- SQLite database
- Automatic API documentation

## Local Installation

```bash
cd backend

python -m venv .venv


## AI Research Agents MVP

After an experiment has a completed `ExperimentResult`, seed the default agents:

```bash
POST /api/agents/seed-defaults
```

Run the five research agents:

```bash
POST /api/agents/research/{experiment_id}/run
```

Inspect previous runs:

```bash
GET /api/agents/research/{experiment_id}/runs
GET /api/agents/research/runs/{run_id}
```

Prepare a Moltbook-ready draft without publishing:

```bash
POST /api/agents/research/{experiment_id}/moltbook-draft
```

The agent layer is research-only and has no broker/order execution capability.
