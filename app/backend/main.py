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

# The gate. APP_PASSWORD must be set; fails closed when it is not.
# Entry paths: the /gate page sets a signed long-lived cookie (the front
# door Bryan uses), and a Basic header still works for scripts and tests.
# Five wrong tries from one IP lock it for 15 minutes and email the owner.
import hashlib
import hmac
import time as _time

_app_password = os.environ.get("APP_PASSWORD")
_MAX_BODY_BYTES = 2_000_000
_SESSION_COOKIE = "tc_session"
_LOCK_AFTER = 5
_LOCK_SECONDS = 15 * 60
_gate_failures: dict = {}  # ip -> {"count": int, "locked_until": ts}


def _session_token() -> str:
    return hmac.new(_app_password.encode(), b"tc-desk-session-v1", hashlib.sha256).hexdigest()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() or (request.client.host if request.client else "?"))[:64]


def _is_authed(request: Request) -> bool:
    cookie = request.cookies.get(_SESSION_COOKIE, "")
    if cookie and secrets.compare_digest(cookie, _session_token()):
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        # The try guards ONLY the credential decode. call_next must stay
        # outside it: a route exception swallowed here becomes a bogus 401.
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            _, _, password = decoded.partition(":")
            return secrets.compare_digest(password, _app_password)
        except Exception:
            return False
    return False


def _gate_page(site_name: str, notice: str = "") -> str:
    notice_html = f'<p class="notice">{notice}</p>' if notice else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{site_name}</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
        background:#0b0c0e; color:#9ba1a9; font-family:-apple-system,"Segoe UI",sans-serif; }}
  form {{ text-align:center; padding:24px; }}
  .crest {{ width:44px; height:44px; border-radius:11px; background:#191c20;
           border:1px solid rgba(231,233,236,.09); margin:0 auto 16px;
           display:flex; align-items:center; justify-content:center;
           font:600 15px ui-monospace,monospace; color:#e7e9ec; }}
  h1 {{ color:#e7e9ec; font-size:17px; font-weight:600; margin:0 0 4px; }}
  p {{ font-size:13px; margin:0 0 18px; }}
  input {{ display:block; width:240px; margin:0 auto 10px; padding:10px 12px; text-align:center;
          background:#121417; color:#e7e9ec; border:1px solid rgba(231,233,236,.12);
          border-radius:8px; font:500 14px ui-monospace,monospace; letter-spacing:.2em; }}
  input:focus {{ outline:none; border-color:#5c9cf5; }}
  button {{ width:240px; padding:10px; background:#5c9cf5; color:#fff; border:none;
           border-radius:8px; font:500 13.5px inherit; cursor:pointer; }}
  .notice {{ color:#e05252; }}
</style></head><body>
<form method="post" action="/gate">
  <div class="crest">{"".join(w[0] for w in site_name.split()[:2]).upper()}</div>
  <h1>{site_name}</h1>
  <p>Members only. Enter the code.</p>
  {notice_html}
  <input name="code" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">Enter the desk</button>
</form></body></html>"""


def _alarm_email(ip: str, count: int) -> None:
    try:
        from app.backend.routes.desk import _email_feedback
        _email_feedback(
            f"ALARM: {count} failed door-code attempts from IP {ip}. "
            f"That IP is locked for 15 minutes. If this keeps happening, "
            f"change APP_PASSWORD on Render.", "gate")
    except Exception as e:
        print(f"Gate alarm email failed: {e!r}")


@app.middleware("http")
async def gate(request: Request, call_next):
    if not _app_password:
        return Response("Server not configured: APP_PASSWORD is unset.", status_code=503)
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > _MAX_BODY_BYTES:
        return Response("Request body too large.", status_code=413)

    site_name = os.environ.get("SITE_NAME", "AI Equity Analyst")

    if request.url.path == "/gate" and request.method == "POST":
        ip = _client_ip(request)
        state = _gate_failures.get(ip, {"count": 0, "locked_until": 0})
        if _time.time() < state["locked_until"]:
            return Response(_gate_page(site_name, "Too many tries. The door is locked for a while."),
                            status_code=429, media_type="text/html")
        from urllib.parse import parse_qs
        body = (await request.body()).decode("utf-8", "replace")
        code = (parse_qs(body).get("code") or [""])[0]
        if secrets.compare_digest(code, _app_password):
            _gate_failures.pop(ip, None)
            response = Response(status_code=303, headers={"Location": "/"})
            response.set_cookie(_SESSION_COOKIE, _session_token(), max_age=180 * 24 * 3600,
                                httponly=True, samesite="lax",
                                secure=request.url.scheme == "https" or "onrender" in request.url.hostname)
            return response
        state["count"] += 1
        if state["count"] >= _LOCK_AFTER:
            state["locked_until"] = _time.time() + _LOCK_SECONDS
            state["count"] = 0
            _gate_failures[ip] = state
            import asyncio as _asyncio
            await _asyncio.to_thread(_alarm_email, ip, _LOCK_AFTER)
            return Response(_gate_page(site_name, "Too many tries. The door is locked for a while."),
                            status_code=429, media_type="text/html")
        _gate_failures[ip] = state
        return Response(_gate_page(site_name, "That is not the code."),
                        status_code=401, media_type="text/html")

    if _is_authed(request):
        return await call_next(request)

    # Unauthenticated: pages get the gate; API calls get a plain 401.
    wants_page = request.method == "GET" and "text/html" in request.headers.get("accept", "")
    if wants_page:
        return Response(_gate_page(site_name), status_code=200, media_type="text/html")
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="analyst"'})


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
