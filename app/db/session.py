from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models import Base

settings = get_settings()
engine = create_async_engine(settings.resolved_database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_INVOICE_COLUMNS = (
    ("invoice_number", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("kind", "VARCHAR(40) NOT NULL DEFAULT 'manual'"),
    ("project_name", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("buyer_vat_id", "VARCHAR(64)"),
    ("buyer_address", "VARCHAR(500)"),
    ("seller_name", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("seller_vat_id", "VARCHAR(64)"),
    ("seller_address", "VARCHAR(500)"),
    ("seller_bank_account", "VARCHAR(64)"),
    ("description", "TEXT"),
    ("period_label", "VARCHAR(80)"),
    ("subtotal_eur", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("vat_rate", "DOUBLE PRECISION NOT NULL DEFAULT 21"),
    ("vat_eur", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("payment_terms_days", "INTEGER NOT NULL DEFAULT 30"),
    ("issued_at", "TIMESTAMPTZ"),
    ("due_date", "TIMESTAMPTZ"),
    ("returned_at", "TIMESTAMPTZ"),
    ("pdf_path", "VARCHAR(500)"),
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for column, definition in _INVOICE_COLUMNS:
            await conn.execute(
                text(f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {column} {definition}")
            )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
