"""
Intain Admin AI - ABS/MBS Structured Finance Application

Backend API built with FastAPI.

Sections:
  /api/deals/extract      - Deal indenture PDF extraction via LLM
  /api/deals              - Deal configuration CRUD
  /api/loantape           - Loantape data from Snowflake
  /api/waterfall          - Payment waterfall computation
  /api/reports            - Investor report generation
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Load environment variables
load_dotenv(Path(__file__).parent / ".env")

# Import routers
from api.deal_extraction import router as extraction_router
from api.deal_config import router as config_router
from api.loantape import router as loantape_router
from api.waterfall import router as waterfall_router
from api.report import router as report_router

# Database
from config.database import create_pool, global_pool

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)

_file_handler = TimedRotatingFileHandler(
    _LOG_DIR / "server.log",
    when="midnight",
    backupCount=7,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console_handler, _file_handler],
    force=True,
)
# Make uvicorn's own loggers use the same handlers (otherwise their lines bypass the file)
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _ulog = logging.getLogger(_name)
    _ulog.handlers = [_console_handler, _file_handler]
    _ulog.propagate = False
# Avoid noisy auto-reload "X change(s) detected" info logs from watchfiles.
for _name in ("watchfiles", "watchfiles.main"):
    logging.getLogger(_name).setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    logger.info("Starting Intain Admin AI backend...")

    # Initialize Snowflake connection
    try:
        await create_pool("ia_demo")
        logger.info("Snowflake connection established")
    except Exception as e:
        logger.warning(f"Snowflake connection failed at startup: {e}. Will retry on first request.")

    # Ensure data directories exist
    from services.deal_store import DEALS_DIR, REPORTS_DIR
    DEALS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Data directories ready: {DEALS_DIR}, {REPORTS_DIR}")

    logger.info("Backend ready. Docs: http://localhost:8010/docs")
    yield

    # Shutdown
    logger.info("Shutting down...")
    if global_pool:
        try:
            global_pool.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Intain Admin AI - Structured Finance API",
    description="""
## ABS/MBS Payment Waterfall Computation Engine

This API powers an end-to-end structured finance workflow:

### Workflow

1. **Upload Deal Indenture** → LLM extracts deal configuration (classes, rates, fees, waterfall rules)
2. **Maker-Checker Review** → User verifies and saves the deal config
3. **Fetch Loantape** → Load loan-level data from Snowflake for a payment date
4. **Compute Waterfall** → Run the payment waterfall calculation
5. **Generate Report** → Create comprehensive investor report (JSON + PDF)

### Key Computations
- Net WAC calculation from loan-level interest rates
- Pass-through rates for floating-rate classes (SOFR + margin, capped at Net WAC)
- Sequential/pro-rata interest and principal distribution
- Collateral performance: CDR, CPR, SMM, delinquency buckets
- Reserve account management
- Trigger test evaluation (clean-up call, OC tests)
- Per-class factors (beginning, interest, principal, ending)
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(extraction_router)
app.include_router(config_router)
app.include_router(loantape_router)
app.include_router(waterfall_router)
app.include_router(report_router)


# ---------------------------------------------------------------------------
# Health check and root
# ---------------------------------------------------------------------------

@app.get("/", tags=["Health"])
async def root():
    """Root endpoint - service health check."""
    return {
        "service": "Intain Admin AI - Structured Finance API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "endpoints": {
            "deal_extraction": "/api/deals/extract",
            "deal_config": "/api/deals",
            "loantape": "/api/loantape",
            "waterfall": "/api/waterfall",
            "reports": "/api/reports",
        },
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Detailed health check."""
    from config.database import global_pool

    db_status = "disconnected"
    if global_pool and not getattr(global_pool, "is_closed", lambda: True)():
        db_status = "connected"
    elif global_pool:
        db_status = "closed"

    return {
        "status": "ok",
        "snowflake": db_status,
        "environment": {
            "azure_openai_endpoint": bool(os.getenv("AZURE_OPENAI_ENDPOINT")),
            "azure_openai_key": bool(os.getenv("AZURE_OPENAI_API_KEY")),
            "role_id": bool(os.getenv("ROLE_ID")),
            "secret_id": bool(os.getenv("SECRET_ID")),
        },
    }


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8010,
        reload=True,
        reload_excludes=["logs/*", "logs/**", "data/*", "data/**"],
        log_level="info",
    )
