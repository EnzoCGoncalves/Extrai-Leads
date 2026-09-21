import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from extrais_leads.api.dependencies import SessionDependency
from extrais_leads.core.logging import log_event
from extrais_leads.models.enums import SearchStatus
from extrais_leads.schemas.search import SearchCreate, SearchRead, SearchResultsPage
from extrais_leads.services.excel_export import EXCEL_CONTENT_TYPE, ExcelExportError
from extrais_leads.services.searches import SearchService

router = APIRouter(prefix="/searches", tags=["searches"])
service = SearchService()
logger = logging.getLogger("extrais_leads.export")


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


@router.get(
    "/{search_id}/export.xlsx",
    response_class=FileResponse,
    summary="Download all persisted search results as Excel",
    responses={
        200: {
            "content": {EXCEL_CONTENT_TYPE: {}},
            "description": "Streaming-friendly XLSX export",
        },
        409: {"description": "Search is still processing"},
    },
)
async def export_search_results(
    search_id: uuid.UUID,
    session: SessionDependency,
    request: Request,
) -> FileResponse:
    search = await service.get(session, search_id)
    if search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="search not found")
    if search.status in {SearchStatus.CREATED, SearchStatus.RUNNING}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="search is still processing",
        )

    try:
        artifact = await request.app.state.excel_export_service.export(session, search)
    except ExcelExportError as exc:
        log_event(
            logger,
            "EXPORT_FAILED",
            "Falha ao gerar exportação Excel",
            level=logging.ERROR,
            search_id=search_id,
            error=type(exc.__cause__ or exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not generate Excel export",
        ) from exc

    log_event(
        logger,
        "EXPORT",
        "Exportação Excel gerada",
        search_id=search_id,
        rows=artifact.row_count,
    )
    try:
        return FileResponse(
            path=artifact.path,
            media_type=EXCEL_CONTENT_TYPE,
            filename=artifact.filename,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Exported-Rows": str(artifact.row_count),
            },
            background=BackgroundTask(artifact.cleanup),
        )
    except Exception:
        artifact.cleanup()
        raise
