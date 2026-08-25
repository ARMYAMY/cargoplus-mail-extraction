import logging
from typing import AsyncGenerator
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import settings
from app.core.limits import MAX_TENANT_CONCURRENCY, MIN_TENANT_CONCURRENCY

# Configure async engine
connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()
logger = logging.getLogger(__name__)


if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for obtaining async DB session in route handlers."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Create tables if not exist and perform light SQLite schema migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.DATABASE_URL.startswith("sqlite"):
            columns = await conn.execute(text("PRAGMA table_info(tenants)"))
            column_names = {row[1] for row in columns}
            if "password_hash" not in column_names:
                await conn.execute(text("ALTER TABLE tenants ADD COLUMN password_hash VARCHAR(128)"))
            if "reserved_balance" not in column_names:
                await conn.execute(
                    text("ALTER TABLE tenants ADD COLUMN reserved_balance NUMERIC(12, 4) NOT NULL DEFAULT 0")
                )

            task_columns = await conn.execute(text("PRAGMA table_info(email_tasks)"))
            task_column_names = {row[1] for row in task_columns}
            if "reserved_amount" not in task_column_names:
                await conn.execute(
                    text("ALTER TABLE email_tasks ADD COLUMN reserved_amount NUMERIC(10, 4) NOT NULL DEFAULT 0")
                )
            if "is_reserved" not in task_column_names:
                await conn.execute(
                    text("ALTER TABLE email_tasks ADD COLUMN is_reserved BOOLEAN NOT NULL DEFAULT 0")
                )
            sqlite_task_migrations = {
                "api_key_id": "ALTER TABLE email_tasks ADD COLUMN api_key_id VARCHAR(64)",
                "idempotency_key": "ALTER TABLE email_tasks ADD COLUMN idempotency_key VARCHAR(128)",
                "last_dispatched_at": "ALTER TABLE email_tasks ADD COLUMN last_dispatched_at DATETIME",
                "lease_owner": "ALTER TABLE email_tasks ADD COLUMN lease_owner VARCHAR(128)",
                "lease_expires_at": "ALTER TABLE email_tasks ADD COLUMN lease_expires_at DATETIME",
                "attempt_count": "ALTER TABLE email_tasks ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
            }
            for column_name, statement in sqlite_task_migrations.items():
                if column_name not in task_column_names:
                    await conn.execute(text(statement))

            few_shot_columns = await conn.execute(text("PRAGMA table_info(few_shot_examples)"))
            few_shot_column_names = {row[1] for row in few_shot_columns}
            if "feedback_id" not in few_shot_column_names:
                await conn.execute(
                    text("ALTER TABLE few_shot_examples ADD COLUMN feedback_id VARCHAR(64)")
                )
            if "source_tenant_id" not in few_shot_column_names:
                await conn.execute(
                    text("ALTER TABLE few_shot_examples ADD COLUMN source_tenant_id VARCHAR(64)")
                )
            if "error_category" not in few_shot_column_names:
                await conn.execute(text("ALTER TABLE few_shot_examples ADD COLUMN error_category VARCHAR(32) DEFAULT 'UNSPECIFIED'"))
            if "lifecycle_status" not in few_shot_column_names:
                await conn.execute(text("ALTER TABLE few_shot_examples ADD COLUMN lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE'"))
            if "evaluation_run_id" not in few_shot_column_names:
                await conn.execute(text("ALTER TABLE few_shot_examples ADD COLUMN evaluation_run_id VARCHAR(64)"))
            if "parent_id" not in few_shot_column_names:
                await conn.execute(text("ALTER TABLE few_shot_examples ADD COLUMN parent_id VARCHAR(64)"))

            benchmark_columns = await conn.execute(text("PRAGMA table_info(benchmark_cases)"))
            benchmark_column_names = {row[1] for row in benchmark_columns}
            if "source_files" not in benchmark_column_names:
                await conn.execute(text("ALTER TABLE benchmark_cases ADD COLUMN source_files JSON"))
            if "source_hashes" not in benchmark_column_names:
                await conn.execute(text("ALTER TABLE benchmark_cases ADD COLUMN source_hashes JSON"))
            if "verification_status" not in benchmark_column_names:
                await conn.execute(text("ALTER TABLE benchmark_cases ADD COLUMN verification_status VARCHAR(32) NOT NULL DEFAULT 'DRAFT'"))
            if "verified_by" not in benchmark_column_names:
                await conn.execute(text("ALTER TABLE benchmark_cases ADD COLUMN verified_by VARCHAR(64)"))
            if "verified_at" not in benchmark_column_names:
                await conn.execute(text("ALTER TABLE benchmark_cases ADD COLUMN verified_at DATETIME"))
            if "dataset_role" not in benchmark_column_names:
                await conn.execute(text("ALTER TABLE benchmark_cases ADD COLUMN dataset_role VARCHAR(16) NOT NULL DEFAULT 'TRAIN'"))
            await conn.execute(text(
                "UPDATE benchmark_cases SET verification_status = 'DRAFT', is_active = 0 "
                "WHERE verified_at IS NULL AND verification_status = 'VERIFIED'"
            ))
            await conn.execute(text(
                "UPDATE benchmark_cases SET is_active = 0 "
                "WHERE verification_status = 'DRAFT'"
            ))

            feedback_columns = await conn.execute(text("PRAGMA table_info(task_feedbacks)"))
            feedback_column_names = {row[1] for row in feedback_columns}
            if "document_type" not in feedback_column_names:
                await conn.execute(text("ALTER TABLE task_feedbacks ADD COLUMN document_type VARCHAR(64) NOT NULL DEFAULT 'GENERAL'"))

            prompt_columns = await conn.execute(text("PRAGMA table_info(prompt_versions)"))
            prompt_column_names = {row[1] for row in prompt_columns}
            if "evidence_feedback_ids" not in prompt_column_names:
                await conn.execute(text("ALTER TABLE prompt_versions ADD COLUMN evidence_feedback_ids JSON"))
            if "iteration_number" not in prompt_column_names:
                await conn.execute(text("ALTER TABLE prompt_versions ADD COLUMN iteration_number INTEGER NOT NULL DEFAULT 1"))
            if "source_job_id" not in prompt_column_names:
                await conn.execute(text("ALTER TABLE prompt_versions ADD COLUMN source_job_id VARCHAR(64)"))
            if "source_evaluation_job_id" not in prompt_column_names:
                await conn.execute(text("ALTER TABLE prompt_versions ADD COLUMN source_evaluation_job_id VARCHAR(64)"))
            await conn.execute(text(
                "UPDATE prompt_versions SET iteration_number = 2 "
                "WHERE parent_id IS NOT NULL AND iteration_number = 1"
            ))

            api_key_columns = await conn.execute(text("PRAGMA table_info(api_keys)"))
            api_key_column_names = {row[1] for row in api_key_columns}
            if "raw_key" not in api_key_column_names:
                await conn.execute(text("ALTER TABLE api_keys ADD COLUMN raw_key VARCHAR(128)"))

            # These indexes make registration and task billing idempotent across processes.
            # Existing installations with duplicate legacy data are left untouched and emit a warning.
            for statement in (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_contact_email "
                "ON tenants(lower(contact_email)) WHERE contact_email IS NOT NULL",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_task_type "
                "ON billing_transactions(task_id, type) WHERE task_id IS NOT NULL",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_tenant_idempotency "
                "ON email_tasks(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS ix_email_tasks_recovery "
                "ON email_tasks(status, lease_expires_at, last_dispatched_at)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_feedback_task_id "
                "ON task_feedbacks(task_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_benchmark_feedback_id "
                "ON benchmark_cases(feedback_id) WHERE feedback_id IS NOT NULL",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_few_shot_feedback_id "
                "ON few_shot_examples(feedback_id) WHERE feedback_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS ix_benchmark_cases_dataset_role "
                "ON benchmark_cases(dataset_role)",
                "CREATE INDEX IF NOT EXISTS ix_few_shot_examples_source_tenant_id "
                "ON few_shot_examples(source_tenant_id)",
            ):
                try:
                    await conn.execute(text(statement))
                except IntegrityError as exc:
                    logger.warning("Could not install uniqueness index; legacy duplicates must be reconciled: %s", exc)
            # SQLite does not enforce the declared NUMERIC precision. These triggers
            # are the final safety boundary for old processes and direct SQL writes.
            money_triggers = (
                """
                CREATE TRIGGER IF NOT EXISTS trg_tenants_money_insert_v1
                BEFORE INSERT ON tenants
                WHEN NEW.balance < 0 OR NEW.balance > 99999999.9999
                  OR NEW.reserved_balance < 0 OR NEW.reserved_balance > NEW.balance
                  OR NEW.unit_price < 0.01 OR NEW.unit_price > 100
                BEGIN
                    SELECT RAISE(ABORT, 'tenant money value outside business limits');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS trg_tenants_money_update_v1
                BEFORE UPDATE OF balance, reserved_balance, unit_price ON tenants
                WHEN NEW.balance < 0 OR NEW.balance > 99999999.9999
                  OR NEW.reserved_balance < 0 OR NEW.reserved_balance > NEW.balance
                  OR NEW.unit_price < 0.01 OR NEW.unit_price > 100
                BEGIN
                    SELECT RAISE(ABORT, 'tenant money value outside business limits');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS trg_billing_money_insert_v1
                BEFORE INSERT ON billing_transactions
                WHEN NEW.amount < 0
                  OR (NEW.type = 'DEDUCTION' AND (NEW.amount < 0.01 OR NEW.amount > 100))
                  OR (NEW.type IN ('RECHARGE', 'REFUND') AND (NEW.amount < 0.01 OR NEW.amount > 1000000))
                  OR NEW.balance_before < 0 OR NEW.balance_before > 99999999.9999
                  OR NEW.balance_after < 0 OR NEW.balance_after > 99999999.9999
                BEGIN
                    SELECT RAISE(ABORT, 'billing transaction outside business limits');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS trg_task_reservation_insert_v1
                BEFORE INSERT ON email_tasks
                WHEN NEW.is_reserved = 1
                 AND (NEW.reserved_amount < 0.01 OR NEW.reserved_amount > 100)
                BEGIN
                    SELECT RAISE(ABORT, 'task reservation outside business limits');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS trg_task_reservation_update_v1
                BEFORE UPDATE OF reserved_amount, is_reserved ON email_tasks
                WHEN NEW.is_reserved = 1
                 AND (NEW.reserved_amount < 0.01 OR NEW.reserved_amount > 100)
                BEGIN
                    SELECT RAISE(ABORT, 'task reservation outside business limits');
                END
                """,
            )
            for statement in money_triggers:
                await conn.execute(text(statement))

            for obsolete_trigger in (
                "trg_tenants_concurrency_insert_v1",
                "trg_tenants_concurrency_update_v1",
            ):
                await conn.execute(text(f"DROP TRIGGER IF EXISTS {obsolete_trigger}"))

            concurrency_triggers = (
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_tenants_concurrency_insert_v2
                BEFORE INSERT ON tenants
                WHEN NEW.max_concurrency < {MIN_TENANT_CONCURRENCY}
                  OR NEW.max_concurrency > {MAX_TENANT_CONCURRENCY}
                  OR NEW.max_concurrency != CAST(NEW.max_concurrency AS INTEGER)
                BEGIN
                    SELECT RAISE(ABORT, 'tenant concurrency must be an integer within business limits');
                END
                """,
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_tenants_concurrency_update_v2
                BEFORE UPDATE OF max_concurrency ON tenants
                WHEN NEW.max_concurrency < {MIN_TENANT_CONCURRENCY}
                  OR NEW.max_concurrency > {MAX_TENANT_CONCURRENCY}
                  OR NEW.max_concurrency != CAST(NEW.max_concurrency AS INTEGER)
                BEGIN
                    SELECT RAISE(ABORT, 'tenant concurrency must be an integer within business limits');
                END
                """,
            )
            for statement in concurrency_triggers:
                await conn.execute(text(statement))
        elif settings.DATABASE_URL.startswith("postgresql"):
            # PostgreSQL schema migrations and indexes
            pg_migrations = (
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS password_hash VARCHAR(128);",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS reserved_balance NUMERIC(12, 4) NOT NULL DEFAULT 0;",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS reserved_amount NUMERIC(10, 4) NOT NULL DEFAULT 0;",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS is_reserved BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS api_key_id VARCHAR(64);",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS last_dispatched_at TIMESTAMPTZ;",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(128);",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;",
                "ALTER TABLE email_tasks ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE few_shot_examples ADD COLUMN IF NOT EXISTS feedback_id VARCHAR(64) "
                "REFERENCES task_feedbacks(id) ON DELETE SET NULL;",
                "ALTER TABLE few_shot_examples ADD COLUMN IF NOT EXISTS source_tenant_id VARCHAR(64) "
                "REFERENCES tenants(id) ON DELETE CASCADE;",
                "ALTER TABLE few_shot_examples ADD COLUMN IF NOT EXISTS error_category VARCHAR(32) DEFAULT 'UNSPECIFIED';",
                "ALTER TABLE few_shot_examples ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE';",
                "ALTER TABLE few_shot_examples ADD COLUMN IF NOT EXISTS evaluation_run_id VARCHAR(64);",
                "ALTER TABLE few_shot_examples ADD COLUMN IF NOT EXISTS parent_id VARCHAR(64);",
                "ALTER TABLE benchmark_cases ADD COLUMN IF NOT EXISTS source_files JSONB;",
                "ALTER TABLE benchmark_cases ADD COLUMN IF NOT EXISTS source_hashes JSONB;",
                "ALTER TABLE benchmark_cases ADD COLUMN IF NOT EXISTS verification_status VARCHAR(32) NOT NULL DEFAULT 'DRAFT';",
                "ALTER TABLE benchmark_cases ADD COLUMN IF NOT EXISTS verified_by VARCHAR(64);",
                "ALTER TABLE benchmark_cases ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;",
                "ALTER TABLE benchmark_cases ADD COLUMN IF NOT EXISTS dataset_role VARCHAR(16) NOT NULL DEFAULT 'TRAIN';",
                "ALTER TABLE task_feedbacks ADD COLUMN IF NOT EXISTS document_type VARCHAR(64) NOT NULL DEFAULT 'GENERAL';",
                "ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS evidence_feedback_ids JSONB;",
                "ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS iteration_number INTEGER NOT NULL DEFAULT 1;",
                "ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS source_job_id VARCHAR(64);",
                "ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS source_evaluation_job_id VARCHAR(64);",
                "UPDATE prompt_versions SET iteration_number = 2 WHERE parent_id IS NOT NULL AND iteration_number = 1;",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS raw_key VARCHAR(128);",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_contact_email ON tenants(lower(contact_email)) WHERE contact_email IS NOT NULL;",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_task_type ON billing_transactions(task_id, type) WHERE task_id IS NOT NULL;",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_tenant_idempotency ON email_tasks(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;",
                "CREATE INDEX IF NOT EXISTS ix_email_tasks_recovery ON email_tasks(status, lease_expires_at, last_dispatched_at);",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_feedback_task_id ON task_feedbacks(task_id);",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_benchmark_feedback_id ON benchmark_cases(feedback_id) WHERE feedback_id IS NOT NULL;",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_few_shot_feedback_id ON few_shot_examples(feedback_id) WHERE feedback_id IS NOT NULL;",
                "CREATE INDEX IF NOT EXISTS ix_benchmark_cases_dataset_role ON benchmark_cases(dataset_role);",
                "CREATE INDEX IF NOT EXISTS ix_few_shot_examples_source_tenant_id ON few_shot_examples(source_tenant_id);",
            )
            for statement in pg_migrations:
                savepoint = await conn.begin_nested()
                try:
                    await conn.execute(text(statement))
                except Exception as exc:
                    await savepoint.rollback()
                    logger.warning("Postgres schema update note: %s", exc)
                else:
                    await savepoint.commit()
            await conn.execute(text(
                "UPDATE benchmark_cases SET verification_status = 'DRAFT', is_active = FALSE "
                "WHERE verified_at IS NULL AND verification_status = 'VERIFIED'"
            ))
            await conn.execute(text(
                "UPDATE benchmark_cases SET is_active = FALSE "
                "WHERE verification_status = 'DRAFT'"
            ))
