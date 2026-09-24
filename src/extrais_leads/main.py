import uvicorn

from extrais_leads.application import create_app
from extrais_leads.core.config import get_settings

app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "extrais_leads.main:app",
        host=settings.app_host,
        port=settings.app_port,
        workers=1,
        reload=settings.app_debug and not settings.is_production,
    )
