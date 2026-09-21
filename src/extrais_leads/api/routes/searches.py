import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status

from extrais_leads.api.dependencies import SessionDependency
from extrais_leads.schemas.search import SearchCreate, SearchRead, SearchResultsPage
from extrais_leads.services.searches import SearchService

router = APIRouter(prefix="/searches", tags=["searches"])
service = SearchService()


@router.post(
    "",
    response_model=SearchRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a search request",
    description=(
        "Persists validated criteria and starts an asynchronous provider search. "
        "Poll GET /searches/{id} for progress and provider diagnostics."
    ),
)
async def create_search(
    payload: SearchCreate,
    session: SessionDependency,
    request: Request,
) -> SearchRead:
    search = await service.create(session, payload)
    response = service.to_read(search)
    if request.app.state.settings.search_background_enabled:
        request.app.state.search_task_manager.start(search.id)
    return response


@router.get("/{search_id}", response_model=SearchRead, summary="Read a search")
async def get_search(search_id: uuid.UUID, session: SessionDependency) -> SearchRead:
    search = await service.get(session, search_id)
    if search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="search not found")
    return service.to_read(search)


@router.get(
    "/{search_id}/results",
    response_model=SearchResultsPage,
    summary="List real results persisted for a search",
)
async def get_search_results(
    search_id: uuid.UUID,
    session: SessionDependency,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> SearchResultsPage:
    search = await service.get(session, search_id)
    if search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="search not found")
    items, total = await service.results(session, search_id, offset=offset, limit=limit)
    return SearchResultsPage(items=items, total=total, limit=limit, offset=offset)
