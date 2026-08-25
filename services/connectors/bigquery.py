"""BigQuery connector — read-only, cost-capped (maximum_bytes_billed).

Auth via a service-account JSON. Introspects a dataset ("project.dataset") through
the BigQuery API (tables + schema + partitioning/clustering + row counts).
"""

from __future__ import annotations

from datetime import UTC, datetime

from google.cloud import bigquery
from google.oauth2 import service_account

from services.connectors.sampling import SampleBudgetExceeded, sample_tables
from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    SAMPLE_BYTES_BUDGET,
    SAMPLE_MAX_ROWS,
    ColumnProfile,
    JoinCandidate,
    PartitioningProfile,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.query_history import QueryRecord
from services.contracts.warehouse import (
    VALIDATION_SCHEMA,
    ColumnSchema,
    DryRunResult,
    QueryResult,
    SchemaSnapshot,
    TableSchema,
)

# Every BigQuery API call carries these bounds: with no transport timeout, a
# read on a pooled keep-alive socket the remote had already closed blocks
# FOREVER — `dst apply` wedges the whole server (parent stuck in read(), /ready
# never answers) and `dst probe` sits at 0% CPU indefinitely. A hang must become
# an error. _API_TIMEOUT_S bounds one HTTP round-trip (connect+read — the
# dead-socket case errors and urllib3 discards the socket, which also stops the
# CLOSE_WAIT accumulation on reuse); _JOB_DEADLINE_S bounds the total wait for
# one query job, generously: real queries finish in seconds, so 600s is not a
# cap anyone hits.
_API_TIMEOUT_S = 60.0
_JOB_DEADLINE_S = 600.0


class BigQueryConnector:
    kind = "bigquery"

    def __init__(
        self,
        credentials_path: str,
        project: str | None = None,
        dataset: str | None = None,
        max_bytes_billed: int = 1_000_000_000,
        sample_bytes_budget: int = SAMPLE_BYTES_BUDGET,
        datasets: list[str] | None = None,
    ) -> None:
        creds = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            credentials_path
        )
        self._configure(creds, project, dataset, max_bytes_billed, sample_bytes_budget, datasets)

    @classmethod
    def from_info(
        cls,
        info: dict[str, object],
        project: str | None = None,
        dataset: str | None = None,
        max_bytes_billed: int = 1_000_000_000,
        sample_bytes_budget: int = SAMPLE_BYTES_BUDGET,
        datasets: list[str] | None = None,
    ) -> BigQueryConnector:
        """Build from a parsed service-account JSON dict (e.g. a decrypted secret)."""
        self = cls.__new__(cls)
        creds = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info
        )
        self._configure(creds, project, dataset, max_bytes_billed, sample_bytes_budget, datasets)
        return self

    def _configure(
        self,
        creds: service_account.Credentials,
        project: str | None,
        dataset: str | None,
        max_bytes_billed: int,
        sample_bytes_budget: int = SAMPLE_BYTES_BUDGET,
        datasets: list[str] | None = None,
    ) -> None:
        self._creds = creds
        self._project = project or self._creds.project_id
        # Dataset pins, each "dataset" or "project.dataset". A real layer reads
        # several, and a single pin silently scoped probe to one dataset's
        # dictionaries — so the plural form is the primary
        # and the old single `dataset` folds into it.
        self._datasets = [d for d in (datasets or ([dataset] if dataset else [])) if d]
        self._dataset = self._datasets[0] if self._datasets else None
        self._max_bytes = max_bytes_billed
        # Public: the pipeline's dry-run gate compares the pre-flight
        # bytes estimate against this cap before any job is started.
        self.max_bytes = max_bytes_billed
        self._sample_bytes_budget = sample_bytes_budget
        self._client = bigquery.Client(credentials=self._creds, project=self._project)

    _MAX_INTROSPECT_TABLES = 250

    def _scope(self) -> tuple[list[bigquery.DatasetReference], str]:
        """The datasets this connection reads, and the label naming that scope.

        The reserved __dst schema stays hidden from introspection even with the
        eval plane removed — a leftover from an older install must never read
        as data."""
        if self._datasets:
            refs = []
            for pin in self._datasets:
                proj, _, ds = pin.rpartition(".")
                refs.append(bigquery.DatasetReference(proj or self._project, ds))
            label = self._datasets[0] if len(self._datasets) == 1 else (self._project or "")
            return refs, label
        refs = [
            d.reference
            for d in self._client.list_datasets(self._project, timeout=_API_TIMEOUT_S)
            if d.dataset_id != VALIDATION_SCHEMA
        ]
        return refs, self._project or ""

    @staticmethod
    def _wanted_match(wanted: set[str], project: str, dataset_id: str, table_id: str) -> bool:
        """Whether a requested name means this table — at any qualification depth."""
        return bool(
            wanted & {table_id, f"{dataset_id}.{table_id}", f"{project}.{dataset_id}.{table_id}"}
        )

    def _table_schema(self, reference: bigquery.TableReference) -> TableSchema:
        tbl = self._client.get_table(reference, timeout=_API_TIMEOUT_S)
        cols = [
            ColumnSchema(
                name=f.name,
                type=f.field_type,
                nullable=f.mode != "REQUIRED",
                description=f.description,
            )
            for f in tbl.schema
        ]
        is_view = tbl.table_type in ("VIEW", "MATERIALIZED_VIEW")
        return TableSchema(
            name=f"{tbl.project}.{tbl.dataset_id}.{tbl.table_id}",
            columns=cols,
            # A view stores no row count — BigQuery reports 0, which prints as
            # "(~0 rows)" for a table with plenty of rows. Unknown is None;
            # external tables don't know their count either.
            row_count=None if tbl.table_type in ("VIEW", "EXTERNAL") else tbl.num_rows,
            is_view=is_view,
            partitioning=tbl.time_partitioning.field if tbl.time_partitioning else None,
            clustering=list(tbl.clustering_fields or []),
            description=tbl.description,
        )

    def introspect(self) -> SchemaSnapshot:
        # With dataset pins introspect just those; without any, introspect every
        # dataset in the project (skipping the __dst plane) so multi-dataset
        # connections still expose their tables to the lens builder. The listing
        # is capped — `truncated` says so, because a capped listing that reads as
        # the whole warehouse poisons every downstream match.
        dataset_refs, label = self._scope()
        tables: list[TableSchema] = []
        truncated = False
        for dataset_ref in dataset_refs:
            if truncated:
                break
            for item in self._client.list_tables(dataset_ref, timeout=_API_TIMEOUT_S):
                if len(tables) >= self._MAX_INTROSPECT_TABLES:
                    truncated = True
                    break
                tables.append(self._table_schema(item.reference))
        return SchemaSnapshot(
            connection=label,
            dialect="bigquery",
            tables=tables,
            schemas_searched=[f"{d.project}.{d.dataset_id}" for d in dataset_refs],
            truncated=truncated,
        )

    def introspect_tables(self, wanted: list[str]) -> SchemaSnapshot:
        """Resolve *wanted* against the FULL catalog — no listing cap.

        `--tables` used to match against the capped universe, so one large
        dataset that sorted first made every other table unreachable by any
        spelling. Listing table names is cheap metadata
        pagination; only matched tables pay a schema fetch.
        """
        dataset_refs, label = self._scope()
        names = {w.strip() for w in wanted if w.strip()}
        tables: list[TableSchema] = []
        for dataset_ref in dataset_refs:
            for item in self._client.list_tables(dataset_ref, timeout=_API_TIMEOUT_S):
                if self._wanted_match(names, item.project, item.dataset_id, item.table_id):
                    tables.append(self._table_schema(item.reference))
        return SchemaSnapshot(
            connection=label,
            dialect="bigquery",
            tables=tables,
            schemas_searched=[f"{d.project}.{d.dataset_id}" for d in dataset_refs],
        )

    # ── CatalogProfiler ───────────────────────────────────────────────────────

    def profile_catalog(self) -> list[TableProfile]:
        """Catalog-only profile from table metadata + INFORMATION_SCHEMA.PARTITIONS.

        No table scans: descriptions/partitioning/clustering/row counts and
        ``Table.modified`` (physical freshness) come from the table resource; the
        latest partition id (a logical-freshness hint) from one cheap PARTITIONS
        query per dataset.
        """
        dataset_refs, _label = self._scope()
        now = datetime.now(UTC)
        profiles: list[TableProfile] = []
        for dataset_ref in dataset_refs:
            if len(profiles) >= self._MAX_INTROSPECT_TABLES:
                break
            latest = self._latest_partitions(dataset_ref)
            for item in self._client.list_tables(dataset_ref, timeout=_API_TIMEOUT_S):
                if len(profiles) >= self._MAX_INTROSPECT_TABLES:
                    break
                tbl = self._client.get_table(item.reference, timeout=_API_TIMEOUT_S)
                partitioning: PartitioningProfile | None = None
                if tbl.time_partitioning is not None:
                    partitioning = PartitioningProfile(
                        column=tbl.time_partitioning.field,
                        kind="time" if tbl.time_partitioning.field else "ingestion",
                        latest_partition=latest.get(tbl.table_id),
                    )
                elif tbl.range_partitioning is not None:
                    partitioning = PartitioningProfile(
                        column=tbl.range_partitioning.field,
                        kind="range",
                        latest_partition=latest.get(tbl.table_id),
                    )
                cols = [
                    ColumnProfile(
                        name=f.name,
                        type=f.field_type,
                        nullable=f.mode != "REQUIRED",
                        description=f.description,
                        description_source="warehouse" if f.description else None,
                    )
                    for f in tbl.schema
                ]
                is_view = tbl.table_type in ("VIEW", "MATERIALIZED_VIEW")
                profiles.append(
                    TableProfile(
                        connection=self._dataset or self._project or "",
                        table=f"{tbl.project}.{tbl.dataset_id}.{tbl.table_id}",
                        description=tbl.description,
                        row_count=None if tbl.table_type in ("VIEW", "EXTERNAL") else tbl.num_rows,
                        is_view=is_view,
                        partitioning=partitioning,
                        clustering=list(tbl.clustering_fields or []),
                        last_updated_physical=tbl.modified,
                        profiled_at=now,
                        source="catalog",
                        columns=cols,
                    )
                )
        return profiles

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        """BigQuery enforces no foreign keys; join inference does this job."""
        return []

    def _latest_partitions(self, dataset_ref: bigquery.DatasetReference) -> dict[str, str]:
        """Best-effort table_id → latest partition id, from INFORMATION_SCHEMA.PARTITIONS.

        One metadata-only query per dataset; permission/region failures degrade to {}.
        """
        sql = (
            "SELECT table_name, MAX(partition_id) AS latest "
            f"FROM `{dataset_ref.project}.{dataset_ref.dataset_id}.INFORMATION_SCHEMA.PARTITIONS` "
            "WHERE partition_id IS NOT NULL AND partition_id != '__NULL__' "
            "GROUP BY table_name"
        )
        try:
            rows = self._client.query(sql, timeout=_API_TIMEOUT_S).result(timeout=_JOB_DEADLINE_S)
            return {str(r[0]): str(r[1]) for r in rows}
        except Exception:
            return {}

    # ── SamplingProfiler ──────────────────────────────────────────────────────

    def sample_profile(
        self,
        tables: list[TableSampleSpec],
        *,
        max_rows: int = SAMPLE_MAX_ROWS,
        max_distinct_for_enum: int = LOW_CARDINALITY_MAX,
    ) -> list[TableProfile]:
        """Guarded sampling pass: ``TABLESAMPLE SYSTEM (x PERCENT)``, dry-run-gated.

        Every sampling query is dry-run first; one whose bytes estimate exceeds
        the sample budget (default ~2 GB) is refused, never run — an over-budget
        table is omitted from the result so its catalog profile stands. Executed
        queries are additionally capped with ``maximum_bytes_billed`` = budget.
        """
        return sample_tables(
            self._sample_query,
            tables,
            connection=self._dataset or self._project or "",
            dialect="bigquery",
            max_rows=max_rows,
            max_distinct_for_enum=max_distinct_for_enum,
        )

    def _sample_query(self, sql: str) -> QueryResult:
        """The dry-run bytes-gate + budget-capped execution for one sampling query."""
        estimate = self.dry_run(sql)
        if not estimate.valid:
            raise RuntimeError(f"sampling query failed dry run: {estimate.error}")
        if (estimate.bytes_estimated or 0) > self._sample_bytes_budget:
            raise SampleBudgetExceeded(
                f"dry run estimates {estimate.bytes_estimated} bytes, "
                f"over the {self._sample_bytes_budget}-byte sampling budget"
            )
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=self._sample_bytes_budget)
        job = self._client.query(sql, job_config=cfg, timeout=_API_TIMEOUT_S)
        rows = job.result(timeout=_JOB_DEADLINE_S)
        columns = [f.name for f in rows.schema]
        data = [list(r.values()) for r in rows]
        return QueryResult(columns=columns, rows=data, bytes_scanned=job.total_bytes_billed)

    def dry_run(self, sql: str) -> DryRunResult:
        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = self._client.query(sql, job_config=cfg, timeout=_API_TIMEOUT_S)
            return DryRunResult(bytes_estimated=job.total_bytes_processed, valid=True)
        except Exception as exc:
            return DryRunResult(valid=False, error=str(exc))

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        query = (
            sql
            if row_limit is None
            else f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _q LIMIT {int(row_limit)}"
        )
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=self._max_bytes)
        job = self._client.query(query, job_config=cfg, timeout=_API_TIMEOUT_S)
        rows = job.result(timeout=_JOB_DEADLINE_S)
        columns = [f.name for f in rows.schema]
        data = [list(r.values()) for r in rows]
        return QueryResult(columns=columns, rows=data, bytes_scanned=job.total_bytes_billed)

    def query_history(self, *, days: int = 30, limit: int = 1000) -> list[QueryRecord]:
        """Distinct SELECT statements from INFORMATION_SCHEMA.JOBS_BY_PROJECT.

        Region-qualified per BigQuery's catalog layout; the dataset's location
        decides the region. Statements + metadata only, never row data. JOBS
        retains 180 days; needs bigquery.jobs.listAll (BigQuery Resource Viewer)
        on the project.
        """
        location = "EU"
        try:
            if self._dataset:
                proj, _, ds = self._dataset.rpartition(".")
                location = self._client.get_dataset(
                    f"{proj or self._project}.{ds}", timeout=_API_TIMEOUT_S
                ).location
            else:
                datasets = list(self._client.list_datasets(self._project, timeout=_API_TIMEOUT_S))
                if datasets:
                    location = self._client.get_dataset(
                        datasets[0].reference, timeout=_API_TIMEOUT_S
                    ).location
        except Exception:  # noqa: BLE001 — fall back to EU; the query below will say if wrong
            pass
        sql = f"""
            SELECT
                query,
                MIN(creation_time) AS first_seen,
                MAX(creation_time) AS last_seen,
                COUNT(*) AS run_count,
                MAX(user_email) AS principal
            FROM `{self._project}.region-{location.lower()}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
            WHERE job_type = 'QUERY'
              AND statement_type = 'SELECT'
              AND state = 'DONE'
              AND error_result IS NULL
              AND creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
              AND NOT REGEXP_CONTAINS(query, r'INFORMATION_SCHEMA|__dst')
            GROUP BY query
            ORDER BY run_count DESC, last_seen DESC
            LIMIT @limit
        """
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("days", "INT64", days),
                bigquery.ScalarQueryParameter("limit", "INT64", limit),
            ]
        )
        rows = self._client.query(sql, job_config=cfg, timeout=_API_TIMEOUT_S).result(
            timeout=_JOB_DEADLINE_S
        )
        return [
            QueryRecord(
                statement=r["query"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                run_count=int(r["run_count"]),
                principal=r["principal"],
                source_tool=None,
            )
            for r in rows
        ]

    def probe_write(self) -> None:
        """Prove write access: create a throwaway table in the dataset, insert, drop."""
        import secrets

        if not self._dataset:
            raise ValueError("a dataset is required to verify write access")
        proj, _, ds = self._dataset.rpartition(".")
        table = f"`{proj or self._project}.{ds}._dst_write_probe_{secrets.token_hex(4)}`"
        self._client.query(f"CREATE TABLE {table} (probe INT64)", timeout=_API_TIMEOUT_S).result(
            timeout=_JOB_DEADLINE_S
        )
        try:
            self._client.query(
                f"INSERT INTO {table} (probe) VALUES (1)", timeout=_API_TIMEOUT_S
            ).result(timeout=_JOB_DEADLINE_S)
        finally:
            self._client.query(f"DROP TABLE IF EXISTS {table}", timeout=_API_TIMEOUT_S).result(
                timeout=_JOB_DEADLINE_S
            )
