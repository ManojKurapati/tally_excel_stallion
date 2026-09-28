I’ll name the project **`stallion_tally_azure`** and make the `CLAUDE.md` specifically instruct Claude Code to build the local Tally connector, extract the datasets, normalize them, and export them securely to Azure. It will treat the uploaded architecture as the baseline and avoid introducing the cloud bridge/tunnel unless needed later. 

````markdown
# CLAUDE.md

# Project: stallion_tally_azure

## 1. Project Goal

Build a production-ready local application that runs on a Windows machine alongside TallyPrime.

The application must:

1. Connect to TallyPrime through its local XML-over-HTTP interface.
2. Extract accounting datasets from TallyPrime.
3. Parse and normalize the Tally XML responses.
4. Store a local copy of the extracted data.
5. Export/synchronize the normalized dataset to Microsoft Azure.
6. Maintain reliable incremental synchronization.
7. Handle failures, retries, duplicates and partial exports safely.
8. Never expose TallyPrime's port 9000 directly to the internet.
9. Be designed so additional Tally write operations can be added later.

The initial objective is:

TallyPrime → Local Application → Azure

Do NOT build the Direct Tunnel architecture initially.

The application should work entirely from the local machine and make outbound connections to Azure.

---

# 2. Core Architecture

Use this architecture:

```text
                    LOCAL WINDOWS MACHINE
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  ┌──────────────┐                                           │
│  │  TallyPrime  │                                           │
│  │ localhost:9000                                           │
│  └───────┬──────┘                                           │
│          │ XML over HTTP                                    │
│          ▼                                                  │
│  ┌───────────────────────┐                                  │
│  │ stallion_tally_agent  │                                  │
│  │                       │                                  │
│  │ Tally Client          │                                  │
│  │ XML Parser            │                                  │
│  │ Normalizer            │                                  │
│  │ Sync Manager          │                                  │
│  │ Local Database        │                                  │
│  └───────────┬───────────┘                                  │
│              │                                               │
│              │ HTTPS                                         │
└──────────────┼───────────────────────────────────────────────┘
               │
               ▼
        ┌──────────────────┐
        │      AZURE       │
        │                  │
        │ Azure Storage /  │
        │ Database         │
        │                  │
        │ Raw + Normalized │
        │ Dataset          │
        └──────────────────┘
````

The local application is the primary integration component.

TallyPrime must never be directly accessible from the public internet.

---

# 3. Source Architecture

The project is based on the DeployOneTally technical architecture.

Important principles from that architecture:

* TallyPrime exposes XML over HTTP on localhost:9000.
* TallyPrime does not provide native authentication on that interface.
* TallyPrime should not be exposed directly to the internet.
* Data should be normalized into a cloud data plane.
* Synchronization must be idempotent.
* Sync operations should have telemetry.
* Tally XML builders and parsers should be reusable.

The initial implementation uses the local/bridge concept, but instead of implementing the full DeployOne cloud push-job architecture, the immediate goal is exporting the dataset to Azure.

---

# 4. Technology Stack

Use the following stack unless there is a strong technical reason to change it.

## Local application

* Python 3.12+
* FastAPI only if a local API is actually required
* httpx or requests for HTTP
* lxml or Python XML libraries for XML parsing
* SQLAlchemy
* SQLite for local persistence
* Pydantic for data validation
* pytest for testing
* structured logging

## Azure

Prefer:

* Azure Blob Storage / ADLS Gen2 for raw and exported datasets
* Azure SQL or PostgreSQL only if a relational cloud database is required
* Azure Key Vault for production secrets
* Azure Application Insights / Azure Monitor for production telemetry

The first implementation should minimize Azure infrastructure.

A simple initial architecture is:

```text
Local SQLite
      ↓
JSON/Parquet
      ↓
Azure Blob Storage
```

Do not introduce unnecessary Azure services.

---

# 5. Repository Structure

Create the project with this structure:

```text
stallion_tally_azure/
│
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── src/
│   └── stallion_tally/
│       │
│       ├── __init__.py
│       ├── config.py
│       ├── logging.py
│       │
│       ├── tally/
│       │   ├── __init__.py
│       │   ├── client.py
│       │   ├── xml_builder.py
│       │   ├── xml_parser.py
│       │   ├── datasets.py
│       │   └── exceptions.py
│       │
│       ├── models/
│       │   ├── company.py
│       │   ├── group.py
│       │   ├── ledger.py
│       │   ├── stock_item.py
│       │   ├── voucher.py
│       │   └── bill.py
│       │
│       ├── database/
│       │   ├── database.py
│       │   ├── models.py
│       │   └── repositories.py
│       │
│       ├── sync/
│       │   ├── manager.py
│       │   ├── pull.py
│       │   ├── normalize.py
│       │   ├── checkpoint.py
│       │   └── state.py
│       │
│       ├── azure/
│       │   ├── client.py
│       │   ├── blob.py
│       │   └── exporter.py
│       │
│       └── cli/
│           └── main.py
│
├── tests/
│   ├── test_tally_client.py
│   ├── test_xml_parser.py
│   ├── test_normalization.py
│   ├── test_database.py
│   ├── test_sync.py
│   └── test_azure_export.py
│
├── scripts/
│   ├── setup_windows.ps1
│   ├── run_sync.ps1
│   └── install_service.ps1
│
└── data/
    ├── local.db
    ├── raw/
    └── exports/
```

Keep modules small and focused.

---

# 6. TallyPrime Connection

TallyPrime is expected to be available locally at:

```text
http://localhost:9000
```

Create a reusable Tally client.

Example interface:

```python
class TallyClient:

    def is_available(self) -> bool:
        ...

    def get_companies(self):
        ...

    def get_groups(self, company: str):
        ...

    def get_ledgers(self, company: str):
        ...

    def get_stock_items(self, company: str):
        ...

    def get_bills(self, company: str):
        ...

    def get_day_book(
        self,
        company: str,
        from_date: str,
        to_date: str
    ):
        ...
```

Do not scatter raw XML requests throughout the codebase.

All Tally communication must go through this client.

---

# 7. XML Handling

Separate XML generation from XML parsing.

```text
xml_builder.py
    Python object → Tally XML

xml_parser.py
    Tally XML → Python object
```

Never make the database layer understand Tally XML.

The data flow must be:

```text
Tally XML
    ↓
Parser
    ↓
Validated Python model
    ↓
Local database
    ↓
Azure exporter
```

---

# 8. Initial Datasets

The initial implementation must support:

1. Companies
2. Groups
3. Ledgers
4. Stock Items
5. Bills Outstanding
6. Day Book / Vouchers

Do not attempt to support every Tally dataset initially.

The first successful end-to-end test should use:

```text
Companies
Ledgers
Vouchers
Bills
```

---

# 9. Voucher Extraction

Voucher extraction should initially use the Day Book.

Use a configurable date range.

Default:

```text
90 days
```

Do not hard-code 90 days throughout the application.

Use configuration:

```env
TALLY_VOUCHER_LOOKBACK_DAYS=90
TALLY_VOUCHER_CHUNK_SIZE=500
```

The application must process vouchers in chunks.

Example:

```text
90 days
    ↓
500 records
    ↓
500 records
    ↓
500 records
    ↓
...
```

Never load an unnecessarily large Tally response into memory.

---

# 10. Local Database

SQLite is the initial local persistence layer.

The local database is important.

Do NOT treat Azure as the only copy.

The local database should maintain:

* normalized records
* synchronization status
* checkpoints
* last successful sync
* record hashes
* export status
* errors

Example:

```text
sync_state

dataset
company
last_successful_sync
last_export
status
error
```

---

# 11. Idempotency

Synchronization must be idempotent.

Running:

```text
sync
sync
sync
sync
```

must not create duplicate records.

Use stable identifiers wherever Tally provides them.

For vouchers:

```text
company + tally_guid
```

For masters:

```text
company + natural key
```

Use database unique constraints.

Do not rely only on application-level duplicate checks.

---

# 12. Raw Data Preservation

Preserve the original Tally response when practical.

Store raw exports under:

```text
data/raw/
```

Example:

```text
data/raw/
    2026-09-28/
        companies.xml
        ledgers.xml
        vouchers_001.xml
        vouchers_002.xml
        bills.xml
```

Raw data is useful for:

* debugging
* auditability
* parser improvements
* reproducing synchronization failures

Do not store credentials or secrets in raw files.

---

# 13. Azure Export

The initial Azure export should use Azure Blob Storage.

Recommended structure:

```text
stallion-tally-data/
│
├── raw/
│   └── {company}/
│       └── {dataset}/
│           └── {date}/
│
├── normalized/
│   └── {company}/
│       └── {dataset}/
│           └── {date}/
│
└── manifests/
    └── {company}/
        └── {date}/
```

Example:

```text
normalized/
    Stallion Automotive/
        ledgers/
            2026-09-28/
                part-001.parquet

        vouchers/
            2026-09-28/
                part-001.parquet

        bills/
            2026-09-28/
                part-001.parquet
```

Prefer Parquet for normalized analytical datasets.

Use JSON only where it makes the data easier to consume.

---

# 14. Azure Authentication

Development may use:

```text
AZURE_STORAGE_CONNECTION_STRING
```

Production should prefer:

```text
Azure Managed Identity
```

or another Azure-native identity mechanism.

Never hard-code:

* Azure credentials
* storage keys
* connection strings
* Tally credentials
* API keys

into source code.

Use:

```text
.env
```

locally and Azure Key Vault / Managed Identity in production.

---

# 15. Export Manifest

Every export should produce a manifest.

Example:

```json
{
  "company": "Stallion Automotive",
  "dataset": "vouchers",
  "exported_at": "2026-09-28T10:00:00Z",
  "record_count": 500,
  "file": "part-001.parquet",
  "checksum": "...",
  "status": "success"
}
```

This allows us to verify whether a dataset successfully reached Azure.

---

# 16. Sync Pipeline

The complete synchronization pipeline should be:

```text
1. Check Tally availability
        ↓
2. Discover companies
        ↓
3. Extract dataset
        ↓
4. Save raw response
        ↓
5. Parse XML
        ↓
6. Validate records
        ↓
7. Normalize records
        ↓
8. Upsert SQLite
        ↓
9. Create export batch
        ↓
10. Upload to Azure
        ↓
11. Verify upload
        ↓
12. Update sync state
```

If step 10 fails:

```text
Do NOT mark the sync as completed.
```

The local dataset should remain available for retry.

---

# 17. Failure Handling

The system must tolerate:

* Tally not running
* Tally becoming unavailable
* malformed XML
* empty datasets
* network failure
* Azure failure
* timeout
* duplicate records
* partial uploads
* process restart
* Windows machine restart

Never silently swallow exceptions.

Every failure should have:

```text
timestamp
dataset
company
operation
error
retry_count
```

---

# 18. Retry Policy

Use exponential backoff.

Example:

```text
attempt 1 → 2 seconds
attempt 2 → 5 seconds
attempt 3 → 15 seconds
attempt 4 → 30 seconds
attempt 5 → 60 seconds
```

Make retry counts configurable.

Do not retry permanent validation errors indefinitely.

---

# 19. Logging

Use structured logs.

Example:

```text
INFO  Tally connection successful
INFO  Company discovered: Stallion Automotive
INFO  Extracting dataset=vouchers
INFO  Parsed records=500
INFO  Local upsert successful records=500
INFO  Azure upload successful file=part-001.parquet
```

Errors:

```text
ERROR Azure upload failed
company=Stallion Automotive
dataset=vouchers
attempt=3
error="..."
```

Never log secrets.

---

# 20. CLI

Create a simple CLI.

Commands:

```bash
stallion-tally test-connection

stallion-tally companies

stallion-tally sync

stallion-tally sync --dataset ledgers

stallion-tally sync --dataset vouchers

stallion-tally sync --from 2026-01-01 --to 2026-09-28

stallion-tally export

stallion-tally status

stallion-tally health
```

The CLI should be usable by a non-developer operator.

---

# 21. Windows Deployment

The application is intended to run on Windows machines where TallyPrime is installed.

Provide:

```text
scripts/setup_windows.ps1
scripts/install_service.ps1
```

The final application should ideally run as a Windows service.

Desired behavior:

```text
Windows starts
      ↓
Stallion Tally Agent starts
      ↓
Checks Tally
      ↓
Waits if Tally unavailable
      ↓
Starts synchronization when available
```

Do not require a developer to manually run Python commands after installation.

---

# 22. Scheduling

Initial default:

```text
sync every 5 minutes
```

Make this configurable.

Example:

```env
SYNC_INTERVAL_SECONDS=300
```

Do not use aggressive polling against Tally.

If Tally is unavailable, use backoff rather than continuously hammering localhost:9000.

---

# 23. Data Model

Normalized models should contain the original Tally identifiers wherever available.

Example:

```python
class Ledger:
    company_id: str
    tally_guid: str | None
    name: str
    parent_group: str | None
    raw_data: dict | None
```

Do not throw away fields from Tally simply because the first application does not use them.

Store important raw/extended fields where practical.

---

# 24. Multi-Company Support

One Tally installation may contain multiple companies.

The synchronization system must therefore always maintain:

```text
company_id
```

on every dataset.

Never assume the active Tally company is the only company.

The pipeline should explicitly identify the company before requesting company-specific datasets.

---

# 25. Security Requirements

Minimum requirements:

* No public exposure of port 9000.
* No inbound network connection to Tally.
* HTTPS for Azure communication.
* Secrets stored outside source code.
* Local database should not contain cloud credentials in plaintext.
* Never log access tokens.
* Validate all external input.
* Validate Tally responses before persistence.
* Use least-privilege Azure permissions.
* Use unique Azure storage paths per tenant/company.

---

# 26. AI Integration

AI is NOT part of the first synchronization MVP.

Do not use an LLM for:

* XML parsing
* accounting calculations
* record identification
* duplicate detection
* voucher construction
* data synchronization

These operations must be deterministic.

AI can be added later for:

```text
Natural language financial queries
Financial analysis
Anomaly detection
Workflow automation
Document understanding
Human approval workflows
```

The accounting data layer must remain deterministic underneath the AI layer.

---

# 27. Development Rules

Claude Code must:

1. Inspect the existing repository before modifying it.
2. Never overwrite working code without understanding it.
3. Keep modules small.
4. Use type hints.
5. Use Pydantic models for external data.
6. Use SQLAlchemy for database access.
7. Write tests for core functionality.
8. Avoid unnecessary dependencies.
9. Never hard-code secrets.
10. Never hard-code company names.
11. Never assume a single Tally company.
12. Never expose Tally's port publicly.
13. Prefer deterministic code over LLM-based logic.
14. Keep Azure-specific code isolated under `azure/`.
15. Keep Tally-specific code isolated under `tally/`.

---

# 28. Testing Strategy

Tests should exist at four levels.

## Unit tests

Test:

```text
XML parsing
XML generation
data normalization
validation
database upserts
deduplication
```

## Integration tests

Test:

```text
Tally → parser → database
database → exporter → Azure
```

## Failure tests

Simulate:

```text
Tally unavailable
malformed XML
Azure unavailable
duplicate records
timeout
process restart
```

## End-to-end test

Run:

```text
TallyPrime
    ↓
local agent
    ↓
SQLite
    ↓
Parquet
    ↓
Azure Blob
```

and verify record counts and checksums.

---

# 29. Implementation Order

Claude Code should implement in this order.

## Phase 1 — Foundation

Create:

```text
project structure
configuration
logging
SQLite
models
CLI
```

Do not build Azure yet.

## Phase 2 — Tally Connectivity

Implement:

```text
TallyClient
XML requests
XML parser
company discovery
```

First success criterion:

```bash
stallion-tally test-connection
```

returns a successful Tally connection.

## Phase 3 — Dataset Extraction

Implement:

```text
companies
groups
ledgers
stock items
bills
vouchers
```

## Phase 4 — Local Persistence

Implement:

```text
normalized models
SQLite repositories
upserts
unique constraints
sync state
```

## Phase 5 — Azure Export

Implement:

```text
Azure Blob client
Parquet exporter
upload
manifest
checksum
retry
```

## Phase 6 — Scheduling

Implement:

```text
automatic sync
incremental sync
checkpointing
retry
Windows service
```

## Phase 7 — Production Hardening

Implement:

```text
structured logs
health checks
metrics
failure recovery
installation scripts
configuration validation
```

---

# 30. Definition of Done

The first production MVP is complete when:

1. TallyPrime is running on a Windows PC.
2. `stallion-tally` can connect to Tally on localhost:9000.
3. The application discovers all available companies.
4. It extracts companies, groups, ledgers, stock items, bills and vouchers.
5. Data is parsed and normalized.
6. Data is stored locally.
7. Running sync multiple times does not create duplicates.
8. The normalized dataset is exported to Azure Blob Storage.
9. Azure exports contain manifests and checksums.
10. Failed uploads can be retried.
11. Tally being offline does not crash the application.
12. Windows can restart without losing synchronization state.
13. No Tally port is exposed to the internet.
14. Secrets are not present in source code.
15. The entire system can be installed and operated without opening the source code.

---

# 31. Important Product Direction

This project should be treated as the foundational accounting data connector for Stallion's future financial operating system.

The architecture should therefore separate:

```text
Tally Integration
        ↓
Accounting Data Layer
        ↓
Azure Data Layer
        ↓
Business Applications
        ↓
AI / Agents
```

Do not tightly couple the Tally connector to any specific AI application.

The long-term goal is that Tally becomes one data source and the normalized Azure dataset becomes the common financial data layer used by Stallion's applications.

---

# 32. First Task for Claude Code

Before writing significant application code:

1. Inspect the repository.
2. Create the directory structure.
3. Create `pyproject.toml`.
4. Create `.env.example`.
5. Implement configuration management.
6. Implement the SQLite database.
7. Implement `TallyClient`.
8. Implement a `test-connection` CLI command.
9. Add tests.
10. Run the tests.
11. Only then begin dataset extraction.

Do not implement the entire system in one pass.

Build one vertical slice first:

```text
Tally
  ↓
Companies
  ↓
XML Parser
  ↓
SQLite
  ↓
Azure Blob
```

Once that works end-to-end, replicate the pattern for the remaining datasets.

```

The most important change from the earlier architecture is that **Azure becomes the destination data layer**, while the Windows machine acts as the secure Tally-side collector. This keeps the initial system much simpler than deploying the full inbound tunnel architecture. :contentReference[oaicite:1]{index=1}
```
