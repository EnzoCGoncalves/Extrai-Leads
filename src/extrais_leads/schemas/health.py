from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"
    application: str
    version: str
    environment: str
    database: Literal["up"] = "up"
