from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import base64
import logging
import asyncio
import os
import secrets
from pathlib import Path

from fastapi import Request, Response

from app.backend.routes import api_router
from app.backend.database.connection import engine
from app.backend.database.models import Base
from app.backend.services.ollama_service import ollama_service

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Hedge Fund API", description="Backend API for AI Hedge Fund", version="0.1.0")

# Initialize database tables (this is safe to run multiple times)
Base.metadata.create_all(bind=engine)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Frontend URLs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Password gate. APP_PASSWORD must be set; any username works, the password
# must match. Fails closed: an unset password never exposes the site.
_app_password = os.environ.get("APP_PASSWORD")
_MAX_BODY_BYTES = 2_000_000


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not _app_password:
        return Response("Server not configured: APP_PASSWORD is unset.", status_code=503)
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > _MAX_BODY_BYTES:
        return Response("Request body too large.", status_code=413)
    auth = request.headers.get("Authorization", "")
    authed = False
    if auth.startswith("Basic "):
        # The try guards ONLY the credential decode. call_next must stay
        # outside it: a route exception swallowed here becomes a bogus 401.
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            _, _, password = decoded.partition(":")
            authed = secrets.compare_digest(password, _app_password)
        except Exception:
            authed = False
    if not authed:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="analyst"'})
    return await call_next(request)


# Include all routes
app.include_router(api_router)


@app.get("/site-config")
def site_config():
    """Personalization for the report UI, driven by environment variables."""
    return {
        "site_name": os.environ.get("SITE_NAME", "AI Equity Analyst"),
        "tagline": os.environ.get("SITE_TAGLINE", "The investment committee of legends"),
        "birthday_message": os.environ.get("BIRTHDAY_MESSAGE", ""),
    }


# Serve the report UI (registered after the API routes, so they take precedence)
_web_dir = Path(__file__).resolve().parents[2] / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")

@app.on_event("startup")
async def startup_event():
    """Startup event to check Ollama availability."""
    try:
        logger.info("Checking Ollama availability...")
        status = await ollama_service.check_ollama_status()
        
        if status["installed"]:
            if status["running"]:
                logger.info(f"✓ Ollama is installed and running at {status['server_url']}")
                if status["available_models"]:
                    logger.info(f"✓ Available models: {', '.join(status['available_models'])}")
                else:
                    logger.info("ℹ No models are currently downloaded")
            else:
                logger.info("ℹ Ollama is installed but not running")
                logger.info("ℹ You can start it from the Settings page or manually with 'ollama serve'")
        else:
            logger.info("ℹ Ollama is not installed. Install it to use local models.")
            logger.info("ℹ Visit https://ollama.com to download and install Ollama")
            
    except Exception as e:
        logger.warning(f"Could not check Ollama status: {e}")
        logger.info("ℹ Ollama integration is available if you install it later")
