import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from extrais_leads.models import ResultSource, Search, SearchProviderRun, SearchResult


class SearchRepository:
    async def add(self, session: AsyncSession, search: Search) -> Search:
        session.add(search)
        await session.flush()
        return search

    async def get(self, session: AsyncSession, search_id: uuid.UUID) -> Search | None:
        statement = (
            select(Search)
            .where(Search.id == search_id)
            .options(selectinload(Search.provider_runs).selectinload(SearchProviderRun.source))
        )
        return await session.scalar(statement)

    async def list_results(
        self,
        session: AsyncSession,
        search_id: uuid.UUID,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[SearchResult], int]:
        total = await session.scalar(
            select(func.count(SearchResult.id)).where(SearchResult.search_id == search_id)
        )
        results = await self.list_result_batch(
            session,
            search_id,
            offset=offset,
            limit=limit,
        )
        return results, int(total or 0)

    async def list_result_batch(
        self,
        session: AsyncSession,
        search_id: uuid.UUID,
        *,
        offset: int,
        limit: int,
    ) -> list[SearchResult]:
        """Load one bounded, fully-hydrated result batch without recounting rows."""

        statement = (
            select(SearchResult)
            .where(SearchResult.search_id == search_id)
            .options(
                selectinload(SearchResult.company),
                selectinload(SearchResult.sources).selectinload(ResultSource.source),
                selectinload(SearchResult.contact_evidences),
            )
            .order_by(SearchResult.rank.asc().nullslast(), SearchResult.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
        return list((await session.scalars(statement)).all())
