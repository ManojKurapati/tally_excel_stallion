# stallion_tally_azure

Local Windows agent that connects to **TallyPrime** (XML over HTTP on `localhost:9000`),
extracts accounting datasets, normalises them into a local SQLite database and exports
them as Parquet files (with manifests and checksums) to **Azure Blob Storage**.

```text
TallyPrime (localhost:9000) --> stallion-tally agent --> SQLite --> Parquet --> Azure Blob Storage
```

Tally is never exposed to the internet: the agent runs on the Tally machine and only
makes outbound HTTPS connections to Azure.

## Datasets

| Dataset | Source in Tally | Key |
|---|---|---|
| companies | Company collection | company GUID |
| groups | List of Accounts (Groups) | company + name |
| ledgers | List of Accounts (Ledgers) | company + name |
| stock_items | List of Accounts (Stock Items) | company + name |
| bills | Outstanding bills (TDL collection, falls back to Bills Receivable/Payable reports) | company + ledger + bill ref |
| vouchers | Day Book, in date chunks | company + voucher GUID |
| voucher_ledger_entries / voucher_inventory_entries | exploded from vouchers | company + voucher GUID + line |

Every record keeps the original Tally fields in `raw_json`, a `record_hash`, and an
`is_deleted` flag so the Azure dataset is an append-only change log: each Parquet part
contains inserted/updated rows; deletions arrive as rows with `is_deleted = true`.

## Requirements

* Windows 10/11 or Windows Server with **TallyPrime** installed
* **Python 3.12 or newer** (`winget install Python.Python.3.12`)
* An Azure Storage account (or run with `AZURE_STORAGE_BACKEND=local` to test without Azure)

### TallyPrime configuration

1. Open TallyPrime and load the company (or companies) to export.
2. Press **F1 (Help) > Settings > Connectivity > Client/Server configuration**.
3. Set **TallyPrime acts as** = `Both` (or `Server`), **Enable ODBC** = `Yes`, **Port** = `9000`.
4. Restart TallyPrime. Only companies that are *open* in Tally can be extracted.

Do **not** open port 9000 on the Windows firewall for external networks.

## Installation on the Windows machine

1. Copy this folder to the machine, e.g. `C:\StallionTally`.
2. Open **PowerShell** in that folder and run:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\scripts\setup_windows.ps1
   ```

   The script creates `.venv`, installs the agent, creates `.env` and runs `test-connection`.
3. Edit `.env` (see below), then run the first sync:

   ```powershell
   .\scripts\run_sync.ps1
   .\.venv\Scripts\stallion-tally.exe status
   ```
4. Install the background agent (Administrator PowerShell):

   ```powershell
   .\scripts\install_service.ps1              # scheduled task at boot, restarts automatically
   .\scripts\install_service.ps1 -Mode Service -NssmPath C:\tools\nssm.exe   # real Windows service (NSSM)
   .\scripts\install_service.ps1 -Uninstall
   ```

## Configuration (`.env`)

All settings are documented in [`.env.example`](.env.example). The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `TALLY_HOST` / `TALLY_PORT` | `localhost` / `9000` | Tally XML server |
| `TALLY_COMPANIES` | *(all)* | comma-separated allow-list of company names |
| `TALLY_VOUCHER_LOOKBACK_DAYS` | `90` | Day Book window re-pulled by `sync` |
| `TALLY_VOUCHER_CHUNK_DAYS` | `7` | size of each Day Book request |
| `TALLY_VOUCHER_CHUNK_SIZE` | `500` | records per database batch |
| `SYNC_INTERVAL_SECONDS` | `300` | agent interval |
| `SYNC_FULL_REFRESH_INTERVAL_SECONDS` / `SYNC_INCREMENTAL_DAYS` | `3600` / `7` | the agent re-pulls the full window hourly and the last 7 days in between |
| `AZURE_STORAGE_BACKEND` | `azure` | `azure`, `local` (folder), or `disabled` |
| `AZURE_STORAGE_CONNECTION_STRING` | | development / simple deployments |
| `AZURE_STORAGE_ACCOUNT_URL` | | production with Managed Identity (`https://<account>.blob.core.windows.net`) |
| `AZURE_STORAGE_CONTAINER` | `stallion-tally-data` | container name |
| `AZURE_UPLOAD_RAW` | `true` | also upload raw Tally XML under `raw/` |
| `EXPORT_BATCH_SIZE` | `500` | records per Parquet part |

Secrets live only in `.env` (never committed) or in Azure Managed Identity / Key Vault.
`stallion-tally config` prints the effective configuration with secrets masked.

## Commands

```text
stallion-tally test-connection                 check Tally and list loaded companies
stallion-tally companies                       list companies with their ids
stallion-tally sync                            full pipeline for all datasets
stallion-tally sync --dataset ledgers          one dataset (repeatable)
stallion-tally sync --company "Stallion Automotive"
stallion-tally sync --from 2026-01-01 --to 2026-09-28
stallion-tally sync --no-export                SQLite only
stallion-tally export                          upload pending records, retry failed uploads
stallion-tally status                          sync state, pending exports, recent errors
stallion-tally health [--json] [--max-age-minutes 30]
stallion-tally run [--once]                    background agent loop
stallion-tally config                          effective configuration (secrets masked)
```

Exit codes: `0` ok, `1` failed/partial, `2` Tally unavailable, `3` configuration problem.

## What lands in Azure

```text
stallion-tally-data/
├── normalized/{company}/{dataset}/{YYYY-MM-DD}/part-001.parquet
├── raw/{company}/{dataset}/{YYYY-MM-DD}/{dataset}_{HHMMSS}_{seq}.xml
└── manifests/{company}/{YYYY-MM-DD}/{dataset}-part-001.json
```

Each manifest records the company, dataset, record count, file, `sha256` checksum,
`Content-MD5`, batch id and run id. Blob metadata carries the same checksum, and the
agent verifies size and MD5 after every upload before marking records exported.

## Local data

```text
data/local.db         SQLite: normalized records, sync_state, checkpoints, export batches, errors
data/raw/             raw Tally responses (retention RAW_RETENTION_DAYS)
data/exports/         Parquet parts + manifests (retention EXPORT_RETENTION_DAYS)
data/logs/            JSON log files (rotating)
data/health.json      summary of the last run
```

A sync is only marked `completed` after the export has been verified. If Azure is
unreachable, the data stays in SQLite with `export_failed` state and `stallion-tally export`
(or the next agent cycle) retries the same Parquet files.

## How the pipeline works

1. Check Tally availability (`GET http://localhost:9000`).
2. Discover loaded companies (TDL collection of type `Company`).
3. For each company and dataset: send the XML request, stream the response to `data/raw/`,
   parse it (sanitising Tally's non-standard XML), validate with Pydantic models.
4. Normalise to rows with a `record_hash`; upsert into SQLite with
   `INSERT ... ON CONFLICT DO UPDATE` (unique constraints on the natural keys).
   Unchanged rows are not rewritten or re-exported; rows missing from a complete pull
   are soft-deleted (bills become `settled`).
5. Create export batches of pending rows, write Parquet, upload, verify, upload manifest,
   mark rows exported, update `sync_state`.

Retries use exponential backoff (`RETRY_BACKOFF_SECONDS`, default 2, 5, 15, 30, 60 s).
Validation and authentication errors are not retried.

## Development

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

A step-by-step Windows test plan is in [`docs/WINDOWS_TEST_PLAN.md`](docs/WINDOWS_TEST_PLAN.md).

The test suite runs a fake TallyPrime (`tests/conftest.py`) and a local storage backend,
so no Tally installation or Azure account is needed.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `test-connection` fails | TallyPrime open? Client/Server = Both? Port matches `TALLY_PORT`? Try `http://localhost:9000` in a browser: it should show *TallyPrime Server is Running*. |
| `Companies loaded in Tally: 0` | Open the company in Tally (Alt+F3 / Select Company). |
| `Could not set 'SVCurrentCompany'` | The company was closed in Tally between discovery and extraction. |
| `export_failed` in `status` | Check `AZURE_*` in `.env`, run `stallion-tally health`, then `stallion-tally export`. |
| Bills empty | The TDL `Bills` collection is not supported by your Tally release; the agent falls back to the Bills Receivable/Payable reports. Inspect `data/raw/<date>/<company>/bills_*.xml`. |
| Parser problems | Raw responses are kept in `data/raw/`; attach the file when reporting the issue. |
