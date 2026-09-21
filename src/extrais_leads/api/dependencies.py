from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from extrais_leads.db import Database


def get_database(request: Request) -> Database:
    return request.app.state.database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]
