# GTM System: Google Sheets + n8n

## Project Overview
Building a Go-To-Market (GTM) automation system that combines:
- **Google Sheets** as the data layer (via **Rowbound** CLI for enrichment pipelines)
- **n8n** as the workflow automation engine

## Architecture
- **Rowbound** handles per-row data enrichment in Google Sheets (formulas, HTTP APIs, waterfalls, AI, etc.)
- **n8n** orchestrates multi-step workflows — webhooks, triggers, integrations, and automation logic
- Together they form a GTM pipeline: lead capture -> enrichment -> outreach/actions

## Rowbound Setup (on this machine)
- **Rowbound v1.7.3** — built from source at `C:\Users\jaivardhan\rowbound-src`, globally linked
- **Google Workspace CLI (gws) v0.22.5** — installed globally
- **GCP Project**: `rowbound-sheets-553137`
- **Active Sheet**: `1JkLX-RbwpW-yOtnbKTBylxCAnE-hcTffkt2c8wxFm6E` (Rowbound - Lead Enrichment)
- Env vars in `~/.bashrc` (GOOGLE_WORKSPACE_CLI_CLIENT_ID, GOOGLE_WORKSPACE_CLI_CLIENT_SECRET)
- Windows fix applied in `sheets-adapter.ts` (`cmd /c gws`)

## n8n Setup
- n8n instance details: TBD
- Workflows: TBD

## Key Commands
```bash
# Rowbound
rowbound run <sheetId>              # Run enrichment pipeline
rowbound run <sheetId> --dry-run    # Preview without writing
rowbound config show <sheetId>      # View sheet config
rowbound config add-action <sheetId> --json '{...}'  # Add action
rowbound watch <sheetId>            # Auto-run on sheet changes

# n8n
# Add n8n commands as workflows are built
```

## Conventions
- Store API keys via `rowbound env set KEY=value`, not in code
- Use formula actions for simple transforms, http/waterfall for external data
- n8n workflows should be exported as JSON and stored in this repo
- Keep sheet IDs and credentials out of committed files
