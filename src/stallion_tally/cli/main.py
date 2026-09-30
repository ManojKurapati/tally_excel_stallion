"""stallion-tally command line interface (usable by a non-developer operator)."""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import click

from stallion_tally import __version__
from stallion_tally.config import SYNC_DATASETS, Settings
from stallion_tally.logging import configure_logging, get_logger

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TALLY_UNAVAILABLE = 2
EXIT_CONFIG = 3


@dataclass
class AppContext:
    settings: Settings
    _database: Any = None
    _client: Any = None
    _storage: Any = None
    _storage_built: bool = False

    @property
    def database(self) -> Any:
        if self._database is None:
            from stallion_tally.database import Database

            self.settings.ensure_directories()
            self._database = Database(self.settings.resolved_database_url)
            self._database.init_schema()
            from stallion_tally.sync.state import SyncStateRepository

            with self._database.session() as session:
                interrupted = SyncStateRepository(session).mark_interrupted_runs()
            if interrupted:
                log.warning(
                    "Previous sync run was interrupted (process or machine restart)",
                    runs=interrupted,
                )
        return self._database

    @property
    def client(self) -> Any:
        if self._client is None:
            from stallion_tally.tally.client import TallyClient

            s = self.settings
            self._client = TallyClient(
                s.tally_base_url,
                timeout=s.tally_timeout_seconds,
                connect_timeout=s.tally_connect_timeout_seconds,
                retry_attempts=s.retry_max_attempts,
                backoff_schedule=s.backoff_schedule,
            )
        return self._client

    @property
    def storage(self) -> Any:
        if not self._storage_built:
            from stallion_tally.azure.client import build_storage

            self._storage = build_storage(self.settings)
            self._storage_built = True
        return self._storage

    def manager(self) -> Any:
        from stallion_tally.sync.manager import SyncManager

        return SyncManager(self.settings, self.database, self.client, self.storage)


def _load_settings(env_file: str | None, data_dir: str | None, log_level: str | None) -> Settings:
    overrides: dict[str, Any] = {}
    if data_dir:
        overrides["app_data_dir"] = Path(data_dir)
    if log_level:
        overrides["log_level"] = log_level
    if env_file:
        return Settings(_env_file=env_file, **overrides)  # type: ignore[call-arg]
    return Settings(**overrides)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%SZ")
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def print_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    rows = [[_fmt(v) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    click.echo(line)
    click.echo("  ".join("-" * w for w in widths))
    for row in rows:
        click.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    if not rows:
        click.echo("(no rows)")


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--env-file", type=click.Path(dir_okay=False), help="Path to the .env file (default: ./.env)."
)
@click.option("--data-dir", type=click.Path(file_okay=False), help="Override APP_DATA_DIR.")
@click.option(
    "--log-level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False)
)
@click.option("--json-logs", is_flag=True, help="Emit JSON logs on the console.")
@click.version_option(__version__, prog_name="stallion-tally")
@click.pass_context
def cli(
    ctx: click.Context,
    env_file: str | None,
    data_dir: str | None,
    log_level: str | None,
    json_logs: bool,
) -> None:
    """Stallion Tally agent: extract TallyPrime data, store it locally and export it to Azure."""
    try:
        settings = _load_settings(env_file, data_dir, log_level)
    except Exception as exc:  # noqa: BLE001 - configuration errors must be readable
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    settings.ensure_directories()
    # Only the commands that change data write to the rotating log file, so that
    # `status`/`health` never compete with the running agent for the file on Windows.
    writes_data = ctx.invoked_subcommand in {"sync", "export", "run"}
    configure_logging(
        settings.log_level,
        "json" if json_logs else settings.log_format,
        settings.log_dir,
        settings.log_to_file and writes_data,
    )
    ctx.obj = AppContext(settings)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@cli.command("test-connection")
@click.pass_obj
def test_connection(app: AppContext) -> None:
    """Check that TallyPrime answers on the configured host/port."""
    from stallion_tally.tally.exceptions import TallyError
    from stallion_tally.tally.xml_parser import parse_companies

    client = app.client
    click.echo(f"Tally URL: {client.base_url}")
    try:
        info = client.server_info()
        click.echo(f"Tally responded: {info or '(empty response)'}")
        response = client.get_companies()
        companies = parse_companies(response.raw_bytes())
    except TallyError as exc:
        click.echo(f"FAILED: {exc}", err=True)
        click.echo(
            "Check that TallyPrime is running, that 'TallyPrime acts as' is set to Server/Both "
            "(F1 Help > Settings > Connectivity) and that the port matches TALLY_PORT.",
            err=True,
        )
        sys.exit(EXIT_TALLY_UNAVAILABLE)
    click.echo(f"Companies loaded in Tally: {len(companies)}")
    for company in companies:
        guid = company.tally_guid or "-"
        click.echo(f"  - {company.name}  (guid={guid}, books from {company.books_from or '-'})")
    if not companies:
        click.echo("WARNING: no company is open in Tally. Open a company before running sync.")
    click.echo("Connection OK")


@cli.command("companies")
@click.pass_obj
def companies(app: AppContext) -> None:
    """List the companies currently loaded in Tally."""
    from stallion_tally.tally.exceptions import TallyError
    from stallion_tally.tally.xml_parser import parse_companies

    try:
        found = parse_companies(app.client.get_companies().raw_bytes())
    except TallyError as exc:
        click.echo(f"FAILED: {exc}", err=True)
        sys.exit(EXIT_TALLY_UNAVAILABLE)
    allow = {c.lower() for c in app.settings.company_allowlist}
    print_table(
        ["Company", "Company ID", "Books from", "Ending at", "Currency", "Selected"],
        [
            (
                c.name,
                c.company_id,
                c.books_from,
                c.ending_at,
                c.base_currency_symbol,
                (not allow) or c.name.lower() in allow,
            )
            for c in found
        ],
    )


def _parse_date(_ctx: click.Context, _param: click.Parameter, value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise click.BadParameter("use YYYY-MM-DD") from exc


@cli.command("sync")
@click.option(
    "--dataset",
    "-d",
    "datasets",
    multiple=True,
    type=click.Choice(SYNC_DATASETS, case_sensitive=False),
    help="Only sync these datasets (repeatable).",
)
@click.option(
    "--company",
    "-c",
    "companies_opt",
    multiple=True,
    help="Only sync these companies (name or id, repeatable).",
)
@click.option(
    "--from", "from_date", callback=_parse_date, help="Voucher window start (YYYY-MM-DD)."
)
@click.option("--to", "to_date", callback=_parse_date, help="Voucher window end (YYYY-MM-DD).")
@click.option("--no-export", is_flag=True, help="Sync to SQLite only; do not upload to Azure.")
@click.option(
    "--excel", "with_excel", is_flag=True, help="Also write the Excel verification workbook(s)."
)
@click.pass_obj
def sync(
    app: AppContext,
    datasets: tuple[str, ...],
    companies_opt: tuple[str, ...],
    from_date: date | None,
    to_date: date | None,
    no_export: bool,
    with_excel: bool,
) -> None:
    """Run the full pipeline: Tally -> SQLite -> Azure."""
    if from_date and to_date and to_date < from_date:
        raise click.BadParameter("--to must not be before --from")
    problems = app.settings.validation_problems() if not no_export else []
    if problems:
        for problem in problems:
            click.echo(f"Configuration problem: {problem}", err=True)
        sys.exit(EXIT_CONFIG)
    result = app.manager().run(
        datasets=datasets or None,
        companies=companies_opt or None,
        from_date=from_date,
        to_date=to_date,
        export=not no_export,
        trigger="cli",
    )
    _print_run(result)
    if with_excel and result.status != "tally_unavailable":
        click.echo("")
        _write_excel_reports(app, companies_opt, from_date, to_date, None)
    if result.status in {"completed", "completed_no_export"}:
        sys.exit(EXIT_OK)
    sys.exit(EXIT_TALLY_UNAVAILABLE if result.status == "tally_unavailable" else EXIT_FAILED)


def _print_run(result: Any) -> None:
    click.echo("")
    click.echo(
        f"Run {result.run_id}  status={result.status}"
        + (f"  ({result.message})" if result.message else "")
    )
    if result.companies:
        click.echo("Companies: " + ", ".join(result.companies))
    print_table(
        [
            "Company",
            "Dataset",
            "Status",
            "Inserted",
            "Updated",
            "Unchanged",
            "Deleted",
            "Rejected",
            "Window",
            "Error",
        ],
        [
            (
                r.company_name,
                r.dataset,
                r.status,
                r.inserted,
                r.updated,
                r.unchanged,
                r.deleted,
                r.rejected,
                f"{r.window_from}..{r.window_to}" if r.window_from else None,
                (r.error or "")[:80] or None,
            )
            for r in result.results
        ],
    )
    if result.export is not None:
        e = result.export
        click.echo(
            f"Export: files verified={e.verified} failed={e.failed} records={e.records} "
            f"raw uploaded={e.raw_uploaded} raw failed={e.raw_failed}"
            + (f"  error={e.error}" if e.error else "")
        )


@cli.command("export")
@click.option(
    "--dataset", "-d", "datasets", multiple=True, help="Only export these datasets (repeatable)."
)
@click.option(
    "--company",
    "-c",
    "companies_opt",
    multiple=True,
    help="Only export these company ids (repeatable).",
)
@click.pass_obj
def export(app: AppContext, datasets: tuple[str, ...], companies_opt: tuple[str, ...]) -> None:
    """Upload pending local records (and retry failed uploads) to Azure."""
    from stallion_tally.azure.exporter import Exporter, export_dataset_names
    from stallion_tally.sync.state import SyncStateRepository

    problems = app.settings.validation_problems()
    if problems:
        for problem in problems:
            click.echo(f"Configuration problem: {problem}", err=True)
        sys.exit(EXIT_CONFIG)
    storage = app.storage
    if storage is None:
        click.echo("Export is disabled (AZURE_STORAGE_BACKEND=disabled).", err=True)
        sys.exit(EXIT_CONFIG)
    run_id = str(uuid.uuid4())
    with app.database.session() as session:
        run = SyncStateRepository(session).start_run("export", list(datasets) or ["all"])
        run_id = run.run_id
    summary = Exporter(app.database, storage, app.settings).export_pending(
        run_id,
        company_ids=list(companies_opt) or None,
        datasets=export_dataset_names(datasets) if datasets else None,
    )
    status = "completed" if summary.ok else "failed"
    with app.database.session() as session:
        state = SyncStateRepository(session)
        state.finish_run(run_id, status, None, summary.error)
        if summary.ok:
            for row in state.all():
                if row.status in {"pending_export", "export_failed"}:
                    state.mark_completed(row.dataset, row.company_id)
    print_table(
        ["Company", "Dataset", "File", "Records", "Status", "Error"],
        [
            (
                f.company_name,
                f.dataset,
                f.file_name,
                f.record_count,
                f.status,
                (f.error or "")[:80] or None,
            )
            for f in summary.files
        ],
    )
    click.echo(
        f"Export {status}: verified={summary.verified} failed={summary.failed} "
        f"raw uploaded={summary.raw_uploaded} raw failed={summary.raw_failed}"
        + (f"  error={summary.error}" if summary.error else "")
    )
    sys.exit(EXIT_OK if summary.ok else EXIT_FAILED)


def _write_excel_reports(
    app: AppContext,
    companies_opt: Sequence[str],
    from_date: date | None,
    to_date: date | None,
    output_dir: Path | None,
) -> list[Any]:
    """Write one verification workbook per selected company from the local database."""
    from stallion_tally.database.repositories import CompanyRepository
    from stallion_tally.reports import load_company_report, write_company_workbook
    from stallion_tally.utils.dates import utcnow

    target = output_dir or (app.settings.export_dir / "excel")
    wanted = {c.lower() for c in companies_opt}
    generated_at = utcnow()
    results = []
    with app.database.session() as session:
        companies = [
            c
            for c in CompanyRepository(session).all()
            if not c.is_deleted
            and (
                not wanted
                or c.company_name.lower() in wanted
                or c.company_id.lower() in wanted
            )
        ]
        if not companies:
            click.echo(
                "No matching company in the local database. Run 'stallion-tally sync' first"
                + (" or check the --company value." if wanted else "."),
                err=True,
            )
            return results
        for company in companies:
            data = load_company_report(session, company, from_date, to_date)
            result = write_company_workbook(data, target, generated_at)
            results.append(result)
            log.info(
                "Excel verification workbook written",
                company=company.company_name,
                file=str(result.path),
            )
    print_table(
        ["Company", "Vouchers", "Ledgers", "Checks failed", "Checks warn", "File"],
        [
            (
                r.company_name,
                r.sheet_rows.get("Day Book", 0),
                r.sheet_rows.get("Ledgers", 0),
                r.failed_checks,
                r.warning_checks,
                str(r.path),
            )
            for r in results
        ],
    )
    return results


@cli.command("excel")
@click.option(
    "--company",
    "-c",
    "companies_opt",
    multiple=True,
    help="Only these companies (name or id, repeatable). Default: all synced companies.",
)
@click.option(
    "--from", "from_date", callback=_parse_date, help="Voucher period start (YYYY-MM-DD)."
)
@click.option("--to", "to_date", callback=_parse_date, help="Voucher period end (YYYY-MM-DD).")
@click.option(
    "--output",
    "-o",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Folder for the workbooks (default: <data dir>/exports/excel).",
)
@click.pass_obj
def excel(
    app: AppContext,
    companies_opt: tuple[str, ...],
    from_date: date | None,
    to_date: date | None,
    output_dir: Path | None,
) -> None:
    """Write Excel workbooks for a Tally expert to verify the extracted data.

    Reads the local database only (TallyPrime does not need to be running).
    Without --from/--to the period is the last Day Book window synced from Tally.
    """
    if from_date and to_date and to_date < from_date:
        raise click.BadParameter("--to must not be before --from")
    results = _write_excel_reports(app, companies_opt, from_date, to_date, output_dir)
    sys.exit(EXIT_OK if results else EXIT_FAILED)


@cli.command("status")
@click.option(
    "--errors",
    "error_count",
    default=10,
    show_default=True,
    help="Number of recent errors to show.",
)
@click.pass_obj
def status(app: AppContext, error_count: int) -> None:
    """Show synchronisation state, pending exports and recent errors."""
    from stallion_tally.config import EXPORT_DATASETS
    from stallion_tally.database.repositories import ExportRepository
    from stallion_tally.sync.state import SyncStateRepository

    with app.database.session() as session:
        state = SyncStateRepository(session)
        export_repo = ExportRepository(session)
        last = state.last_run()
        click.echo(f"Database: {app.settings.resolved_database_url}")
        if last:
            click.echo(
                f"Last run: {last.run_id} status={last.status} trigger={last.trigger} "
                f"started={_fmt(last.started_at)} finished={_fmt(last.finished_at)}"
            )
        else:
            click.echo("Last run: never")
        click.echo("")
        print_table(
            [
                "Company",
                "Dataset",
                "Status",
                "Last sync",
                "Last export",
                "Records",
                "Errors",
                "Last error",
            ],
            [
                (
                    s.company_name or s.company_id,
                    s.dataset,
                    s.status,
                    s.last_successful_sync,
                    s.last_export,
                    s.records_last_run,
                    s.error_count,
                    (s.last_error or "")[:60] or None,
                )
                for s in state.all()
            ],
        )
        click.echo("")
        pending = {d: export_repo.pending_count(d) for d in EXPORT_DATASETS}
        click.echo("Pending export records: " + ", ".join(f"{d}={n}" for d, n in pending.items()))
        counts = export_repo.batch_counts_by_status()
        click.echo(
            "Export batches: " + (", ".join(f"{k}={v}" for k, v in counts.items()) or "none")
        )
        errors = state.recent_errors(error_count)
        if errors:
            click.echo("")
            print_table(
                ["Time", "Company", "Dataset", "Operation", "Retries", "Error"],
                [
                    (
                        e.timestamp,
                        e.company_id,
                        e.dataset,
                        e.operation,
                        e.retry_count,
                        e.error[:100],
                    )
                    for e in errors
                ],
            )


@cli.command("health")
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.option(
    "--max-age-minutes",
    default=None,
    type=int,
    help="Fail if the last successful run is older than this.",
)
@click.pass_obj
def health(app: AppContext, as_json: bool, max_age_minutes: int | None) -> None:
    """Health check: configuration, database, Tally and storage. Exit code 0 = healthy."""
    from stallion_tally.azure.blob import StorageError
    from stallion_tally.sync.state import SyncStateRepository
    from stallion_tally.utils.dates import utcnow

    checks: dict[str, dict[str, Any]] = {}
    problems = app.settings.validation_problems()
    checks["config"] = {"ok": not problems, "detail": "; ".join(problems) or "ok"}

    try:
        writable = app.database.is_writable()
        checks["database"] = {"ok": writable, "detail": app.settings.resolved_database_url}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": str(exc)}

    try:
        info = app.client.server_info()
        checks["tally"] = {"ok": True, "detail": info or "responding"}
    except Exception as exc:  # noqa: BLE001
        checks["tally"] = {"ok": False, "detail": str(exc)}

    if app.settings.azure_storage_backend == "disabled":
        checks["storage"] = {"ok": True, "detail": "export disabled"}
    else:
        try:
            storage = app.storage
            storage.ensure_container()
            checks["storage"] = {"ok": True, "detail": storage.describe()}
        except (StorageError, Exception) as exc:  # noqa: BLE001
            checks["storage"] = {"ok": False, "detail": str(exc)}

    last_ok: datetime | None = None
    try:
        with app.database.session() as session:
            run = SyncStateRepository(session).last_successful_run()
            last_ok = run.finished_at if run else None
    except Exception:  # noqa: BLE001
        pass
    age_minutes = (utcnow() - last_ok).total_seconds() / 60 if last_ok else None
    stale = max_age_minutes is not None and (age_minutes is None or age_minutes > max_age_minutes)
    checks["last_successful_run"] = {
        "ok": not stale,
        "detail": _fmt(last_ok) if last_ok else "never",
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
    }

    healthy = all(c["ok"] for c in checks.values())
    if as_json:
        click.echo(
            json.dumps({"healthy": healthy, "checks": checks, "version": __version__}, indent=2)
        )
    else:
        for name, check in checks.items():
            click.echo(f"[{'OK' if check['ok'] else 'FAIL'}] {name}: {check['detail']}")
        click.echo("HEALTHY" if healthy else "UNHEALTHY")
    sys.exit(EXIT_OK if healthy else EXIT_FAILED)


@cli.command("run")
@click.option("--once", is_flag=True, help="Run a single cycle and exit (waits for Tally first).")
@click.pass_obj
def run(app: AppContext, once: bool) -> None:
    """Run the background agent: wait for Tally and sync every SYNC_INTERVAL_SECONDS."""
    from stallion_tally.sync.scheduler import Agent

    problems = app.settings.validation_problems()
    if problems:
        for problem in problems:
            click.echo(f"Configuration problem: {problem}", err=True)
        sys.exit(EXIT_CONFIG)
    agent = Agent(app.settings, app.manager(), app.client)
    result = agent.run_forever(once=once)
    if once and result is not None:
        _print_run(result)
        sys.exit(EXIT_OK if result.ok else EXIT_FAILED)


@cli.command("config")
@click.pass_obj
def config(app: AppContext) -> None:
    """Show the effective configuration (secrets masked) and validation problems."""
    for key, value in app.settings.describe().items():
        click.echo(f"{key} = {value}")
    problems = app.settings.validation_problems()
    click.echo("")
    if problems:
        for problem in problems:
            click.echo(f"PROBLEM: {problem}")
        sys.exit(EXIT_CONFIG)
    click.echo("Configuration OK")


@cli.command("init")
@click.pass_obj
def init(app: AppContext) -> None:
    """Create the data directories and the local database."""
    app.settings.ensure_directories()
    _ = app.database
    click.echo(f"Data directory: {app.settings.app_data_dir}")
    click.echo(f"Database: {app.settings.resolved_database_url}")
    click.echo("Initialised")


def main() -> None:
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
