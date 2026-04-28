from collections.abc import AsyncGenerator

import structlog
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = structlog.get_logger(__name__)

engine: AsyncEngine | None = None
async_session_maker: async_sessionmaker[AsyncSession] | None = None

def init_db(db_url: str):
    global engine, async_session_maker
    logger.info("initializing_database_engine_dynamically")
    engine = create_async_engine(
        db_url,
        echo=False,
        future=True,
    )
    async_session_maker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

if settings.DATABASE_URL:
    init_db(settings.DATABASE_URL)

async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    if not async_session_maker:
        raise HTTPException(status_code=503, detail="DATABASE_NOT_CONFIGURED")
    async with async_session_maker() as session:
        yield session
from sqlalchemy import text


async def check_db_ready() -> str:
    """Check if database is ready.
    Returns:
        "ready": Database exists and tables are present with data
        "no_tables": Database exists but tables are missing
        "no_data": Tables exist but are empty (requires seeding)
        "not_configured": Database URL not set
        "error": Connection failed or other error
    """
    if not async_session_maker:
        return "not_configured"
    try:
        async with async_session_maker() as session:
             # Try to count roles to check both table existence and data presence
             result = await session.execute(text("SELECT count(*) FROM role_agents"))
             count = result.scalar()
             if count == 0:
                 return "no_data"
             return "ready"
    except Exception as e:
        error_str = str(e).lower()
        # Common patterns for missing tables in Postgres (asyncpg)
        if "does not exist" in error_str or "undefinedtableerror" in error_str or "42p01" in error_str:
            logger.info("database_accessible_but_tables_missing")
            return "no_tables"
        
        logger.error("db_not_ready_check_failed", error=str(e), type=type(e).__name__)
        return "error"


def _ensure_models_registered():
    """Import all models to ensure metadata is populated for table creation/dropping."""
    from app.models.base import Base
    import app.models.roles
    import app.models.meetings
    import app.models.documents
    return Base

async def create_all_tables():
    """Create all tables defined in the metadata."""
    if engine is None:
        logger.error("cannot_create_tables_engine_none")
        return

    Base = _ensure_models_registered()
    
    async with engine.begin() as conn:
        if settings.DB_SCHEMA:
            logger.info("ensuring_schema_exists", schema=settings.DB_SCHEMA)
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.DB_SCHEMA}"))
        
        logger.info("creating_all_tables_from_metadata")
        await conn.run_sync(Base.metadata.create_all)
        logger.info("all_tables_created_successfully")

async def drop_all_tables():
    if engine is None:
        return

    Base = _ensure_models_registered()

    async with engine.begin() as conn:
        if settings.DB_SCHEMA:
            logger.info("dropping_entire_schema", schema=settings.DB_SCHEMA)
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {settings.DB_SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {settings.DB_SCHEMA}"))
            logger.info("schema_recreated", schema=settings.DB_SCHEMA)
        else:
            await conn.run_sync(Base.metadata.drop_all)
            logger.info("all_tables_dropped")
