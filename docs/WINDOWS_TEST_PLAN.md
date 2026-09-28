# Windows test plan

Step-by-step checks for the first test of the Stallion Tally agent on a Windows PC
that runs TallyPrime. Each step lists the command and what you should see.

## 0. Before you start

* TallyPrime is installed, opened, and the company you want to export is loaded.
* In TallyPrime: **F1 Help > Settings > Connectivity > Client/Server configuration**:
  `TallyPrime acts as = Both`, `Enable ODBC = Yes`, `Port = 9000`. Restart Tally.
* Open `http://localhost:9000` in a browser on the same PC. Expected:
  `TallyPrime Server is Running`. If not, the agent cannot work either.
* Python 3.12 or newer installed (`winget install Python.Python.3.12`), with
  "Add python.exe to PATH" ticked.

## 1. Install

Copy the project folder to `C:\StallionTally`, open PowerShell there and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_windows.ps1
```

Expected at the end: `Connection OK` with the loaded companies listed, and
`Setup finished`. If it prints `FAILED: Tally is not reachable`, go back to step 0.

## 2. First sync without Azure (local backend)

Edit `.env` and set:

```env
AZURE_STORAGE_BACKEND=local
```

Then:

```powershell
.\scripts\run_sync.ps1
```

Expected: a table with one row per company and dataset, all with status `completed`,
and non-zero `Inserted` counts for ledgers and vouchers. Then check:

```powershell
.\.venv\Scripts\stallion-tally.exe status
dir data\azure_local\stallion-tally-data\normalized
dir data\raw
```

* `status` shows every dataset as `completed` with a `Last export` time.
* The `normalized` folder contains one folder per company with `ledgers`, `vouchers`,
  `voucher_ledger_entries`, `bills`, ... and `part-001.parquet` files.
* `data\raw\<date>\<company>\` contains the original Tally XML responses.

Run the sync a second time:

```powershell
.\scripts\run_sync.ps1
```

Expected: `Inserted = 0`, `Updated = 0`, `Unchanged = <same counts as before>`, and
`Export: files verified=0` (nothing changed, nothing re-uploaded).

Change one voucher in Tally (for example edit a narration), run the sync again and
confirm `Updated = 1` for vouchers and one new `part-00N.parquet`.

## 3. Sync to Azure

Create a storage account (or use an existing one) and a container named
`stallion-tally-data` (the agent creates it if `AZURE_CREATE_CONTAINER=true`).
Edit `.env`:

```env
AZURE_STORAGE_BACKEND=azure
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net
AZURE_STORAGE_CONTAINER=stallion-tally-data
```

Then:

```powershell
.\.venv\Scripts\stallion-tally.exe health
.\scripts\run_sync.ps1
```

Expected: `health` shows `[OK] storage`, and the sync ends with
`Export: files verified=N failed=0`. In the Azure portal (Storage browser) you should see:

```text
stallion-tally-data/normalized/<company>/ledgers/<date>/part-001.parquet
stallion-tally-data/manifests/<company>/<date>/ledgers-part-001.json
stallion-tally-data/raw/<company>/ledgers/<date>/ledgers_HHMMSS.xml
```

Open a manifest: `record_count` must match the `Inserted + Updated` count shown by the
sync, and `checksum` must match the `sha256` metadata on the Parquet blob.

## 4. Failure behaviour

1. Close TallyPrime and run `.\scripts\run_sync.ps1`. Expected: status
   `tally_unavailable`, exit code 2, no crash. `status` shows the error under recent errors.
2. Put a wrong account key in `.env` and run a sync. Expected: datasets show
   `export_failed`, `status` shows `Export batches: failed=N`, local data intact.
   Restore the key and run `.\.venv\Scripts\stallion-tally.exe export`. Expected:
   the same `part-00N.parquet` files are uploaded and `status` shows `completed`.

## 5. Run as a background agent

In an **Administrator** PowerShell:

```powershell
.\scripts\install_service.ps1
```

Expected: `Scheduled task 'StallionTallyAgent' installed and started`. Within a minute
`data\logs\stallion_tally.log` shows `Agent started` and then a sync every
`SYNC_INTERVAL_SECONDS` (default 5 minutes). Restart the PC and confirm the task starts
again (`Get-ScheduledTask StallionTallyAgent`) and `status` keeps updating.

Close TallyPrime while the agent runs: the log shows `Tally not available, waiting`
with an increasing `retry_in_seconds`; reopen Tally and syncing resumes.

To remove: `.\scripts\install_service.ps1 -Uninstall`.

## 6. What to send back if something fails

* The output of `stallion-tally status` and `stallion-tally health`.
* `data\logs\stallion_tally.log`.
* The relevant raw file from `data\raw\<date>\<company>\` (for parser problems,
  especially `bills_*.xml`, because the outstanding-bills request has a fallback path
  that must be confirmed against your Tally release).
