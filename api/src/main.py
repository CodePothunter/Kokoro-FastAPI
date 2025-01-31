"""
FastAPI OpenAI Compatible API
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import torch
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .core.config import settings
from .routers.web_player import router as web_router
from .routers.development import router as dev_router
from .routers.openai_compatible import router as openai_router
from .routers.openai_compatible_v2 import router as openai_router_v2
from .routers.debug import router as debug_router


def setup_logger():
    """Configure loguru logger with custom formatting"""
    config = {
        "handlers": [
            {
                "sink": sys.stdout,
                "format": "<fg #2E8B57>{time:hh:mm:ss A}</fg #2E8B57> | "
                "{level: <8} | "
                "{message}",
                "colorize": True,
                "level": "INFO",
            },
        ],
    }
    logger.remove()
    logger.configure(**config)
    logger.level("ERROR", color="<red>")


# Configure logger
setup_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for model initialization"""
    from .inference.model_manager import get_manager
    from .inference.version_manager import get_version_manager
    from .inference.voice_manager import get_manager as get_voice_manager
    from .services.temp_manager import cleanup_temp_files

    # Clean old temp files on startup
    await cleanup_temp_files()

    logger.info("Loading TTS model and voice packs...")

    try:
        # Initialize managers globally
        model_manager = await get_manager()  # For v0.19 compatibility
        version_manager = await get_version_manager()  # For v1.0 support
        voice_manager = await get_voice_manager()

        # Initialize v0.19 model with warmup and get status
        device, model, voicepack_count = await model_manager.initialize_with_warmup(voice_manager)

        # Pre-load v1.0 model
        v1_model = await version_manager.get_model("v1.0")
        logger.info("Initialized v1.0 model")

    except FileNotFoundError:
        logger.error("""
Model files not found! You need to either:

1. Download models using the scripts:
   GPU: python docker/scripts/download_model.py --type pth
   CPU: python docker/scripts/download_model.py --type onnx

2. Set environment variables in docker-compose:
   GPU: DOWNLOAD_PTH=true
   CPU: DOWNLOAD_ONNX=true
""")
        raise
    except Exception as e:
        logger.error(f"Failed to initialize model: {e}")
        raise
        
    boundary = "░" * 2*12
    startup_msg = f"""

{boundary}

    ╔═╗┌─┐┌─┐┌┬┐
    ╠╣ ├─┤└─┐ │ 
    ╚  ┴ ┴└─┘ ┴
    ╦╔═┌─┐┬┌─┌─┐
    ╠╩╗│ │├┴┐│ │
    ╩ ╩└─┘┴ ┴└─┘

{boundary}
                """
    startup_msg += f"\nModel warmed up on {device}: {model}"
    startup_msg += f"\n{voicepack_count} voice packs loaded"
    startup_msg += f"\nKokoro v1.0 model loaded"
    
    # Add web player info if enabled
    if settings.enable_web_player:
        startup_msg += f"\n\nBeta Web Player: http://{settings.host}:{settings.port}/web/"
    else:
        startup_msg += "\n\nWeb Player: disabled"
        
    startup_msg += f"\n{boundary}\n"
    logger.info(startup_msg)

    yield


# Initialize FastAPI app
app = FastAPI(
    title=settings.api_title,
    description=settings.api_description,
    version=settings.api_version,
    lifespan=lifespan,
    openapi_url="/openapi.json",  # Explicitly enable OpenAPI schema
)

# Add CORS middleware if enabled
if settings.cors_enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Include routers
app.include_router(openai_router, prefix="/v1")  # Legacy v0.19 endpoints
app.include_router(openai_router_v2)  # New v2 endpoints with version support
app.include_router(dev_router)  # Development endpoints
app.include_router(debug_router)  # Debug endpoints
if settings.enable_web_player:
    app.include_router(web_router, prefix="/web")  # Web player static files


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}


@app.get("/v1/test")
async def test_endpoint():
    """Test endpoint to verify routing"""
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("api.src.main:app", host=settings.host, port=settings.port, reload=True)
