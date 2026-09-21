from fastapi import APIRouter

from extrais_leads.api.routes.searches import router as searches_router

api_router = APIRouter()
api_router.include_router(searches_router)
