"""Claims API with logs, traces, metrics and audit events.

Run:  uv run uvicorn examples.fastapi.app:app
Env:  PYOBS_TRACING__EXPORTER=otlp PYOBS_TRACING__ENDPOINT=http://localhost:4317 to export.
"""

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

import pyobservability
from pyobservability import audit, get_logger, get_meter, traced
from pyobservability.integrations.fastapi import ObservabilityMiddleware

logger = get_logger("claims")
approved = get_meter("claims").create_counter("claims.approved", unit="{claim}")


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    pyobservability.configure(
        service_name="claims-service",
        service_version="1.0.0",
        auditing={
            "durability": {"boundary": "disk", "spool_dir": ".audit-spool"},
            "retry": {"dead_letter": "file"},
        },
    )
    yield
    await pyobservability.ashutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)


@traced
def decide(claim_id: str) -> str:
    return "APPROVED"


@app.post("/claims/{claim_id}/approve")
async def approve(claim_id: str) -> dict[str, str]:
    status = decide(claim_id)
    approved.add(1, {"channel": "api"})
    logger.info("claim approved", claim_id=claim_id)
    await audit.arecord(
        event_type="CLAIM_APPROVED",
        resource_type="CLAIM",
        resource_id=claim_id,
        action="APPROVE",
        actor_id="USR-1001",
        changes={"status": {"old": "PENDING", "new": status}},
    )
    return {"claim_id": claim_id, "status": status}
