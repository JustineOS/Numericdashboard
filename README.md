# Month-End Close Dashboard

Flask dashboard that tracks outstanding close tasks across all accounting teams by pulling live data from Numeric, applying materiality filtering, and generating Slack status updates.

## What it does

- Pulls all active tasks (flux, reconciliation, checklist) from Numeric via Claude + Numeric MCP
- Filters flux tasks to only material variances (configurable dollar + percentage thresholds)
- Shows who owns each task's next step (Preparer if not started, Reviewer if prep complete)
- Generates one-click Slack messages — detailed per-person or team summary
- Logs completeness data on every sync (raw rows received vs. items after filtering)

## Setup

```bash
pip install -r requirements.txt
python3 app.py
```

Dashboard runs at `http://localhost:5050`

## Syncing data

The dashboard reads from a local cache. To refresh:

1. Ask Claude to sync: *"please refresh the dashboard data"*
2. Claude calls `list_tasks` via Numeric MCP and POSTs the result to `/api/sync`

## Key files

| File | Purpose |
|---|---|
| `app.py` | All business logic |
| `templates/index.html` | Dashboard UI |
| `data/report_config.json` | Materiality thresholds and report classification |
| `data/variance_map.json` | Variance data per account (gitignored) |
| `data/numeric_cache.json` | Cached task data from last sync (gitignored) |
| `data/sync_log.json` | Completeness log (gitignored) |

## ICFR classification

**AI Role:** Builder + Runner | **Lane:** 2 (Intelligent Automation / HOTL)

See [Process Design Document](https://docs.google.com/document/d/19vQ_XpmEuaUiz0F5fSGc8gNDiMo5IQd6zZEITa7k15k) and [Reperformance Log](https://docs.google.com/document/d/14uJ892s0uJCGoLeJgf917abWrzPU76q-JU9fAN_zDQg) for controls documentation.

## Pushing to GitHub (first time)

```bash
# Create a new private repo at github.com, then:
git remote add origin https://github.com/gusto-inc/numeric-close-dashboard.git
git push -u origin main
```
