from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from extrais_leads.models import Base


class Database:
    """Owns the async engine and creates one session per unit of work."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self._prepare_sqlite_directory(url)
        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        if make_url(url).get_backend_name() == "sqlite":
            self._configure_sqlite(self.engine)

    @staticmethod
    def _prepare_sqlite_directory(url: str) -> None:
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite" or not parsed.database:
            return
        if parsed.database in {":memory:", ""}:
            return
        Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _configure_sqlite(engine: AsyncEngine) -> None:
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def initialize(self, *, create_schema: bool = False) -> None:
        if create_schema:
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        else:
            await self.ping()

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
