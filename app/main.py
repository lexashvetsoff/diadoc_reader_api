import sys
from loguru import logger
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.exceptions import ParsingError
from app.api.v1.health import router as health_router
from app.api.v1.parse import router as parce_router

def setup_logging():
    logger.add(sys.stdout, colorize=True, format="<green>{time}</green> <level>{message}</level>")
    logger.add("app//logs/file_{time}.log", format="{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}", rotation="7:00")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Starting {settings.APP_NAME} v{settings.APP_VERSION}")

    yield

    logger.info("👋 Shutting down application")


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title=settings.APP_NAME,
        description=settings.APP_DESCRIPTION,
        version=settings.APP_VERSION,
        lifespan=lifespan
    )

    app.include_router(health_router, prefix="/api/v1", tags=["health"])
    app.include_router(parce_router, prefix="/api/v1", tags=["parce"])

    # @app.exception_handler(ParsingError)
    # async def parsing_error_handler(request, ext):
    #     return JSONResponse(status_code=422, content={"detail": str(ext)})

    return app


app = create_app()
