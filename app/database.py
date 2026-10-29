"""SQLite lifecycle, explicit transaction control, and database audit protection."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.schema import CreateTable
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.db import AuditLog, Base
from app.models.db.constraints import baseline_balance_sql, mission_metadata_sql


class Database:
    def __init__(self, settings: Settings):
        path = Path(make_url(settings.database_url).database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(settings.database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_connection(connection, record):
            # SQLAlchemy owns BEGIN; pragmas run before any transaction begins.
            connection.isolation_level = None
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA recursive_triggers=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
                if cursor.fetchone()[0].lower() != "wal":
                    raise RuntimeError("SQLite WAL mode could not be enabled")
            finally:
                cursor.close()

        @event.listens_for(self.engine.sync_engine, "begin")
        def explicit_begin(connection):
            connection.exec_driver_sql("BEGIN")

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await self._upgrade_ai_provenance(connection)
            await self._upgrade_audit_identity(connection)
            # Guards also protect existing route/mission tables: create_all does
            # not retrofit their CHECK constraints. Never silently repair bad rows.
            for table, predicate in (
                ('route_baselines', baseline_balance_sql()),
                ('missions', mission_metadata_sql()),
            ):
                invalid = (await connection.exec_driver_sql(
                    f'SELECT count(*) FROM {table} WHERE NOT coalesce(({predicate}), 0)'
                )).scalar_one()
                if invalid:
                    raise RuntimeError(f'{table}: {invalid} existing row(s) violate remediation checks; review before startup')
                guarded = baseline_balance_sql('NEW.') if table == 'route_baselines' else mission_metadata_sql('NEW.')
                for operation in ('INSERT', 'UPDATE'):
                    await connection.exec_driver_sql(f"""
                        CREATE TRIGGER IF NOT EXISTS {table}_remediation_{operation.lower()}
                        BEFORE {operation} ON {table}
                        WHEN NOT coalesce(({guarded}), 0)
                        BEGIN SELECT RAISE(ABORT, '{table} remediation validation failed'); END
                    """)
            # These protect writes made through SQLAlchemy AND direct SQL connections.
            for operation in ("UPDATE", "DELETE"):
                await connection.exec_driver_sql(f"""
                    CREATE TRIGGER IF NOT EXISTS audit_logs_no_{operation.lower()}
                    BEFORE {operation} ON audit_logs
                    BEGIN SELECT RAISE(ABORT, 'audit_logs are append-only'); END
                """)
            # WITHOUT ROWID removes the alternate identity; this guards PK replacement.
            await connection.exec_driver_sql("""
                CREATE TRIGGER IF NOT EXISTS audit_logs_no_replace
                BEFORE INSERT ON audit_logs
                WHEN EXISTS (SELECT 1 FROM audit_logs WHERE id = NEW.id)
                BEGIN SELECT RAISE(ABORT, 'audit_logs are append-only'); END
            """)
            # Snapshots and audit details cannot hide floating-point money in JSON.
            for table, column in (("reward_releases", "profitability_snapshot"), ("audit_logs", "details")):
                for operation in ("INSERT", "UPDATE"):
                    await connection.exec_driver_sql(f"""
                        CREATE TRIGGER IF NOT EXISTS {table}_{column}_exact_{operation.lower()}
                        BEFORE {operation} ON {table}
                        WHEN NOT json_valid(NEW.{column})
                          OR EXISTS (SELECT 1 FROM json_tree(NEW.{column}) WHERE type = 'real')
                        BEGIN SELECT RAISE(ABORT, 'snapshot and audit numbers must be exact integers'); END
                    """)

    async def _upgrade_ai_provenance(self, connection) -> None:
        """Add nullable provenance columns without rebuilding or discarding records."""
        columns = {row[1]: row for row in (await connection.exec_driver_sql('PRAGMA table_info(ai_extractions)')).all()}
        for name, sql_type in (("analysis_key", "VARCHAR(160)"),
                               ("request_fingerprint", "VARCHAR(64)"),
                               ("run_state", "VARCHAR(16)"), ("call_details", "JSON")):
            if name not in columns:
                await connection.exec_driver_sql(f'ALTER TABLE ai_extractions ADD COLUMN {name} {sql_type}')
            elif columns[name][2].upper() != sql_type:
                raise RuntimeError('Custom AI provenance schema requires explicit migration review')
        await connection.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_ai_extractions_analysis_key ON ai_extractions(analysis_key)')
        indexes = (await connection.exec_driver_sql('PRAGMA index_list(ai_extractions)')).all()
        target = next(row for row in indexes if row[1] == 'ix_ai_extractions_analysis_key')
        fields = (await connection.exec_driver_sql('PRAGMA index_info(ix_ai_extractions_analysis_key)')).all()
        if not target[2] or [row[2] for row in fields] != ['analysis_key']:
            raise RuntimeError('Custom AI analysis-key index requires explicit migration review')

    async def _upgrade_audit_identity(self, connection) -> None:
        """One targeted, transactional upgrade; preserve every declared audit column."""
        ddl = (await connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
        )).scalar_one()
        if 'WITHOUT ROWID' in ddl.upper():
            return
        expected = {column.name for column in AuditLog.__table__.columns}
        actual = {row[1] for row in (await connection.exec_driver_sql('PRAGMA table_info(audit_logs)')).all()}
        known_triggers = {'audit_logs_no_update', 'audit_logs_no_delete', 'audit_logs_no_replace',
                          'audit_logs_details_exact_insert', 'audit_logs_details_exact_update'}
        objects = (await connection.exec_driver_sql(
            "SELECT type,name FROM sqlite_master WHERE tbl_name='audit_logs' AND sql IS NOT NULL AND type IN ('index','trigger')"
        )).all()
        indexes = (await connection.exec_driver_sql('PRAGMA index_list(audit_logs)')).all()
        if actual != expected or any(row[3] != 'pk' for row in indexes) or any(kind != 'trigger' or name not in known_triggers for kind, name in objects):
            raise RuntimeError('Custom audit schema requires explicit migration review; no data changed')
        tables = (await connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        for table in tables:
            quoted = connection.dialect.identifier_preparer.quote(table)
            if any(row[2] == 'audit_logs' for row in (await connection.exec_driver_sql(f'PRAGMA foreign_key_list({quoted})')).all()):
                raise RuntimeError('Foreign keys referencing audit_logs require explicit migration review')
        if (await connection.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name='audit_logs_rebuild'")).scalar_one():
            raise RuntimeError('audit_logs_rebuild already exists; explicit migration review required')
        statement = str(CreateTable(AuditLog.__table__).compile(dialect=connection.dialect))
        await connection.exec_driver_sql(statement.replace('CREATE TABLE audit_logs (', 'CREATE TABLE audit_logs_rebuild (', 1))
        columns = ', '.join(connection.dialect.identifier_preparer.quote(column.name) for column in AuditLog.__table__.columns)
        await connection.exec_driver_sql(f'INSERT INTO audit_logs_rebuild ({columns}) SELECT {columns} FROM audit_logs')
        await connection.exec_driver_sql('DROP TABLE audit_logs')
        await connection.exec_driver_sql('ALTER TABLE audit_logs_rebuild RENAME TO audit_logs')

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            async with session.begin():
                yield session

    async def close(self) -> None:
        await self.engine.dispose()
