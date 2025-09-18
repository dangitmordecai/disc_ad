from dotenv import load_dotenv
load_dotenv()
import json, os, asyncio, secrets, time, base64, hashlib
from typing import Dict, Any, Optional
from fastapi import Body, HTTPException, Request, BackgroundTasks
from fastapi import FastAPI, HTTPException, Request, Depends, Query, Path as FPath
import re
from sqlalchemy.orm import Session

from sqlalchemy.orm import Session
from config import get_settings
from urllib.parse import urlencode

from starlette.responses import RedirectResponse


from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
import jmespath
from fastapi import Depends
import aiohttp  # needed by notify_bot
from crypto import encrypt_for_user, decrypt_for_user

from fastapi.responses import FileResponse  # add this import
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import Request
from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, Text, DateTime, func
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
from config import get_settings

from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, Text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
from config import get_settings
import re
from typing import Any, Mapping
from fastapi import Depends
from sqlalchemy.orm import Session

SECRET_TAG = re.compile(r"\{\{\s*secret\.([A-Za-z0-9_]+)\s*\}\}")

def _resolve_secret_value(db: Session, user_id: str, guild_id: str | None, key: str) -> str | None:
    # guild-scoped override first
    if guild_id:
        s = db.query(Secret).filter(
            Secret.user_id == user_id,
            Secret.guild_id == guild_id,
            Secret.key == key,
        ).first()
        if s:
            return decrypt_value(s.value_enc)
    # user-wide fallback
    s = db.query(Secret).filter(
        Secret.user_id == user_id,
        Secret.guild_id == None,  # noqa: E711
        Secret.key == key,
    ).first()
    return decrypt_value(s.value_enc) if s else None

def _inject_secrets_in_str(db: Session, user_id: str, guild_id: str | None, s: str) -> str:
    if not s:
        return s
    def repl(m: re.Match[str]) -> str:
        v = _resolve_secret_value(db, user_id, guild_id, m.group(1))
        return v if v is not None else m.group(0)  # leave tag if missing
    return SECRET_TAG.sub(repl, s)

def _inject_secrets_in_obj(db: Session, user_id: str, guild_id: str | None, obj: Any) -> Any:
    # Walk dicts/lists and inject inside any strings
    if isinstance(obj, str):
        return _inject_secrets_in_str(db, user_id, guild_id, obj)
    if isinstance(obj, Mapping):
        return {k: _inject_secrets_in_obj(db, user_id, guild_id, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_inject_secrets_in_obj(db, user_id, guild_id, v) for v in obj]
    return obj

settings = get_settings()
is_sqlite = settings.database_url.startswith("sqlite")



engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if is_sqlite else {}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id       = Column(String, primary_key=True)  # Discord user_id
    username = Column(String)
    avatar   = Column(String)
    email    = Column(String, nullable=True)
    access_token_enc  = Column(Text, nullable=True)   # encrypted access token
    refresh_token_enc = Column(Text, nullable=True)   # encrypted refresh token
    token_expires_at  = Column(Integer, nullable=True) # Unix ts
    token_scopes      = Column(String, nullable=True)
    guilds   = relationship("Guild", back_populates="owner")


class Guild(Base):
    __tablename__ = "guilds"
    id = Column(String, primary_key=True)  # guild_id
    name = Column(String)
    icon = Column(String, nullable=True)
    owner_id = Column(String, ForeignKey("users.id"))
    owner = relationship("User", back_populates="guilds")
    flows = relationship("Flow", back_populates="guild", cascade="all, delete-orphan")
    commands = relationship("Command", back_populates="guild", cascade="all, delete-orphan")

class Flow(Base):
    __tablename__ = "flows"
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String, ForeignKey("guilds.id"), index=True)
    name = Column(String)
    trigger = Column(Text)   # JSON-as-text
    steps = Column(Text)     # JSON-as-text
    enabled = Column(Boolean, default=True)
    guild = relationship("Guild", back_populates="flows")

class Command(Base):
    __tablename__ = "commands"
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String, ForeignKey("guilds.id"), index=True)
    name = Column(String)
    description = Column(Text)
    spec = Column(Text)      # JSON-as-text
    enabled = Column(Boolean, default=True)
    guild = relationship("Guild", back_populates="commands")

class Secret(Base):
    __tablename__ = "secrets"
    # Scope: per user, optionally per guild
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    guild_id = Column(String, index=True, nullable=True)  # null = user-global
    key = Column(String, index=True, nullable=False)      # logical name (e.g. "OPENAI_API_KEY")
    value_enc = Column(Text, nullable=False)              # encrypted value
    created_by = Column(String, nullable=True)
    created_at = Column(String, default=lambda: str(int(time.time())))
    updated_at = Column(String, default=lambda: str(int(time.time())))

class Execution(Base):
    __tablename__ = "executions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String, index=True, nullable=False)
    flow_id = Column(Integer, index=True, nullable=True)     # when a flow ran
    command_id = Column(Integer, index=True, nullable=True)  # when a command ran
    status = Column(String, index=True, nullable=False)      # queued|running|ok|error
    input_ctx = Column(Text, nullable=True)                  # JSON as text
    output_ctx = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(String, default=lambda: str(int(time.time())))
    finished_at = Column(String, nullable=True)

class Log(Base):
    __tablename__ = "logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(Integer, index=True, nullable=False)
    ts = Column(String, default=lambda: str(int(time.time())))
    level = Column(String, default="INFO")
    message = Column(Text, nullable=False)


Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
# ===== end database =====


BASE_DIR   = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
STATIC_DIR = BASE_DIR / "static"


app = FastAPI(title="Discord SaaS API + UI (multi-guild)", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)
# Session middleware …
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "replace-me"),
    same_site="lax",
    https_only=False  # True under HTTPS
)


try:
    import aiohttp
except Exception:
    aiohttp = None

from pathlib import Path
DATA_DIR = Path("data")

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _new_state() -> str:
    return _b64url(secrets.token_bytes(32))

def _new_code_verifier() -> str:
    # 43–128 chars, URL-safe
    return _b64url(secrets.token_bytes(64))

def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return _b64url(digest)

# If you already have these helpers, keep them; otherwise:
def _new_sid() -> str:
    return _b64url(secrets.token_bytes(32))

def _now() -> int:
    import time
    return int(time.time())

async def notify_bot(guild_id: str, kind: str):
    """Tell the bot to live-reload (commands or flows) for a guild."""
    url = os.getenv("BOT_CALLBACK_URL")
    if not url or not aiohttp:
        print("[notify_bot] missing url or aiohttp; url =", url)
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url,
                json={"guild_id": guild_id, "type": kind},
                timeout=5
            ) as r:
                body = await r.text()
                print(f"[notify_bot] POST {url} → {r.status} {body}")
    except Exception as e:
        print("[notify_bot] failed:", e)  # don’t raise; keep Publish fast



# Discord OAuth constants
DISCORD_AUTH = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_API = "https://discord.com/api/v10"

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://127.0.0.1:8000/auth/discord/callback")
OAUTH_SCOPES = os.getenv("OAUTH_SCOPES", "identify guilds")

# Minimal in-memory session store. Keys are random session IDs; values
# contain tokens and user info. Replace with a database in Sprint B.
_SESS: Dict[str, Dict[str, Any]] = {}


# ---- token refresh helper ----
async def _refresh_access_token(refresh_token: str) -> dict | None:
    import httpx
    data = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    async with httpx.AsyncClient() as c:
        r = await c.post(DISCORD_TOKEN, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    return r.json() if r.status_code == 200 else None


def _new_sid() -> str:
    """Generate a new secure session id."""
    return secrets.token_urlsafe(32)

def _now() -> int:
    """Return current Unix time in seconds."""
    return int(time.time())

def get_sid(request: Request) -> str | None:
    """Get the session id stored in the signed cookie."""
    return request.session.get("sid")

def require_login(request: Request) -> Dict[str, Any]:
    sid = get_sid(request)
    if not sid or sid not in _SESS:
        raise HTTPException(status_code=401, detail="Not logged in")
    uid = _SESS[sid].get("user_id")
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == uid).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    # Return user info; tokens stay server‑side
    return {"user": {"id": user.id, "username": user.username, "global_name": user.username.split("#")[0],
                     "avatar": user.avatar, "email": user.email}}


from fastapi import Request
import os

def _bot_ok(request: Request) -> bool:
    want = os.getenv("DASHBOARD_SECRET", "")
    have = request.headers.get("X-DASHBOARD-SECRET", "")
    return bool(want) and have == want

MANAGE_GUILD_BIT = 1 << 5

# at top of file (ensure these are present)
# from crypto import decrypt_for_user
# from sqlalchemy.orm import Session

async def require_guild_access(request: Request, guild_id: str) -> dict:
    """Ensure the logged-in user can manage the target guild (owner or MANAGE_GUILD)."""
    sid = request.session.get("sid")
    if not sid or sid not in _SESS:
        raise HTTPException(status_code=401, detail="Not logged in")

    uid = _SESS[sid].get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="No user in session")

    # Pull encrypted token from DB and decrypt it
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == uid).first()
        if not user or not user.access_token_enc:
            raise HTTPException(status_code=401, detail="No access token on file")
        token = decrypt_for_user(user.id, user.access_token_enc)

    # Call Discord for the user's guild list
    async with httpx.AsyncClient() as c:
        resp = await c.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {token}"}
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to fetch guilds: {resp.text}")

    data = resp.json()
    MANAGE_GUILD_BIT = 1 << 5
    for g in data:
        if g.get("id") != guild_id:
            continue
        owner = bool(g.get("owner"))
        try:
            perms = int(g.get("permissions", "0"))
        except (TypeError, ValueError):
            perms = 0
        if owner or (perms & MANAGE_GUILD_BIT):
            return {"user_id": uid}  # success

    raise HTTPException(status_code=403, detail="You do not manage this guild.")


@app.get("/auth/discord/login")
async def auth_login(request: Request):
    # Ensure we have a sid (used later only to bind the logged-in user id)
    sid = request.session.get("sid")
    if not sid:
        sid = _new_sid()
        request.session["sid"] = sid  # signed cookie

    state = _new_state()
    code_verifier = _new_code_verifier()
    challenge = _code_challenge(code_verifier)

    # Store OAuth one-time values in the signed cookie instead of _SESS
    request.session["oauth_state"] = state
    request.session["code_verifier"] = code_verifier

    params = {
        "client_id":     CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "scope":         OAUTH_SCOPES,
        "state":         state,
        "prompt":        "consent",
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    url = f"{DISCORD_AUTH}?{urlencode(params)}"
    return RedirectResponse(url)


@app.get("/auth/discord/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None):
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")

    # Pull the values from the signed cookie session
    sid = request.session.get("sid")
    cookie_state = request.session.get("oauth_state")
    code_verifier = request.session.get("code_verifier")

    if not sid:
        raise HTTPException(status_code=400, detail="Session not found")
    if state != cookie_state:
        raise HTTPException(status_code=400, detail="Invalid state")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE verifier")

    # --- Exchange code for tokens (must include code_verifier with PKCE) ---
    token_payload = {
        "client_id":     CLIENT_ID,
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": code_verifier,  # REQUIRED when using PKCE
    }

    # If your app has a client secret (most do in dev), include it.
    if CLIENT_SECRET:
        token_payload["client_secret"] = CLIENT_SECRET

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            DISCORD_TOKEN,  # e.g., "https://discord.com/api/v10/oauth2/token"
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    # Always parse safely and bail out with the server’s message
    raw_text = token_resp.text
    try:
        token_data = token_resp.json()
    except Exception:
        token_data = {}

    if token_resp.status_code != 200 or "access_token" not in token_data:
        # Surface Discord’s error so you see the real reason
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {raw_text}")

    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in    = int(token_data.get("expires_in", 0))

    # Fetch user
    async with httpx.AsyncClient() as client:
        me_resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
    if me_resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to fetch user: {me_resp.text}")
    me = me_resp.json()

    # Persist user (uses your existing models/session)
    with SessionLocal() as db:
        u = db.query(User).filter(User.id == me["id"]).first()
        if not u:
            u = User(id=me["id"])
            db.add(u)

        u.username = f"{me['username']}#{me.get('discriminator','0')}"
        u.avatar   = me.get("avatar")
        u.email    = me.get("email")
        u.access_token_enc  = encrypt_for_user(u.id, access_token)
        if refresh_token:
            u.refresh_token_enc = encrypt_for_user(u.id, refresh_token)
        u.token_expires_at   = _now() + int(expires_in or 0)
        u.token_scopes       = token_data.get("scope", "")

        db.commit()
        uid = u.id   # <-- capture while session is open

    # now use uid outside the session safely
    sid = request.session.get("sid") or _new_sid()
    request.session["sid"] = sid
    _SESS[sid] = {"user_id": uid}

    # clear one-time oauth values from cookie session
    request.session.pop("oauth_state", None)
    request.session.pop("code_verifier", None)

    return RedirectResponse(url="/")

@app.post("/auth/logout")
def auth_logout(request: Request):
    """Destroy the current session and remove stored tokens."""
    sid = request.session.get("sid")
    if sid:
        _SESS.pop(sid, None)
    request.session.clear()
    return {"ok": True}

@app.get("/me")
def me_endpoint(request: Request):
    """Return the current user's public profile information."""
    sess = require_login(request)
    return {"user": sess["user"]}

@app.get("/me/guilds")
async def me_guilds(request: Request, include_all: int = 0):
    # 1) session → user id
    sid = request.session.get("sid")
    if not sid or sid not in _SESS:
        raise HTTPException(status_code=401, detail="Not logged in")
    uid = _SESS[sid].get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="No user in session")

    # 2) load tokens from DB
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == uid).first()
        if not user or not user.access_token_enc:
            raise HTTPException(status_code=401, detail="No access token on file")
        access_token = decrypt_for_user(user.id, user.access_token_enc)
        refresh_token = decrypt_for_user(user.id, user.refresh_token_enc) if getattr(user, "refresh_token_enc", None) else None

    async def _fetch_with(token: str):
        async with httpx.AsyncClient() as c:
            return await c.get(f"{DISCORD_API}/users/@me/guilds",
                               headers={"Authorization": f"Bearer {token}"})

    # 3) call Discord; refresh once if needed
    gr = await _fetch_with(access_token)
    if gr.status_code == 401 and refresh_token:
        newt = await _refresh_access_token(refresh_token)
        if newt and "access_token" in newt:
            with SessionLocal() as db:
                u = db.query(User).filter(User.id == uid).first()
                if u:
                    u.access_token_enc = encrypt_for_user(uid, newt["access_token"])
                    if "refresh_token" in newt:
                        u.refresh_token_enc = encrypt_for_user(uid, newt["refresh_token"])
                    u.token_expires_at = _now() + int(newt.get("expires_in", 0) or 0)
                    db.commit()
            gr = await _fetch_with(newt["access_token"])

    if gr.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to fetch guilds: {gr.text}")

    MANAGE_GUILD = 1 << 5
    ADMIN        = 1 << 3
    out = []
    for g in gr.json():
        owner = bool(g.get("owner"))
        try:
            perms = int(g.get("permissions", "0"))
        except (TypeError, ValueError):
            perms = 0
        manageable = owner or (perms & MANAGE_GUILD) or (perms & ADMIN)
        if include_all or manageable:
            out.append({
                "id": g.get("id"),
                "name": g.get("name"),
                "icon": g.get("icon"),
                "owner": owner,
                "permissions": perms,
                "manageable": bool(manageable),
            })
    return {"guilds": out}




from cryptography.fernet import Fernet, InvalidToken

FERNET_KEY = os.getenv("SECRETS_ENC_KEY")
if not FERNET_KEY:
    # Developer safety: don't let secrets be stored unencrypted
    raise RuntimeError("SECRETS_ENC_KEY missing in .env (generate with Fernet.generate_key())")
fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

def encrypt_value(plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()

def decrypt_value(ciphertext: str) -> str:
    return fernet.decrypt(ciphertext.encode()).decode()


from fastapi import Query, Path as FPath

@app.get("/secrets")
def list_secrets(request: Request, guild_id: Optional[str] = Query(None), db: Session = Depends(get_db)):
    sess = require_login(request)
    user_id = sess["user"]["id"]
    q = db.query(Secret).filter(Secret.user_id == user_id)
    if guild_id:
        q = q.filter(Secret.guild_id == guild_id)
    rows = q.all()
    return {
        "secrets": [
            {"id": s.id, "key": s.key, "scope": ("guild" if s.guild_id else "user"), "guild_id": s.guild_id}
            for s in rows
        ]
    }



@app.post("/secrets")
async def upsert_secret(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db)
):
    """
    Body: { "key": "OPENAI_API_KEY", "value": "sk-...", "guild_id": "123" | null }
    """
    sess = require_login(request)
    user_id = sess["user"]["id"]
    key = (payload.get("key") or "").strip()
    value = payload.get("value")
    guild_id = payload.get("guild_id")
    enc_value = encrypt_for_user(user_id, str(value))
    if not key or value is None:
        raise HTTPException(400, "key and value are required")

    if guild_id:
        # ensure the user can manage this guild
        await require_guild_access(request, guild_id)

    # upsert (unique: user_id + guild_id + key)
    row = db.query(Secret).filter(
        Secret.user_id == user_id,
        Secret.guild_id == guild_id,
        Secret.key == key
    ).first()

    enc = encrypt_value(str(value))

    if not row:
        row = Secret(user_id=user_id, guild_id=guild_id, key=key, value_enc=enc, created_by=user_id)
        db.add(row)
    else:
        row.value_enc = enc
        row.updated_at = str(int(time.time()))

    db.commit()
    return {"ok": True, "id": row.id, "scope": ("guild" if guild_id else "user")}

@app.delete("/secrets/{secret_id}")
async def delete_secret(
    request: Request,
    secret_id: int = FPath(...),
    db: Session = Depends(get_db),
):
    sess = require_login(request)
    user_id = sess["user"]["id"]
    row = db.query(Secret).filter(Secret.id == secret_id, Secret.user_id == user_id).first()
    if not row:
        raise HTTPException(404, "Secret not found")

    # If guild-scoped, check access to that guild
    if row.guild_id:
        await require_guild_access(request, row.guild_id)

    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/guilds/{guild_id}/import-from-json")
def import_from_json(guild_id: str, db: Session = Depends(get_db)):
    gdir = DATA_DIR / guild_id
    flows_path = gdir / "flows.json"
    commands_path = gdir / "commands.json"

    flows_doc = _read_json(flows_path, {"flows": []})
    cmds_doc  = _read_json(commands_path, {"commands": []})

    db.query(Flow).filter(Flow.guild_id == guild_id).delete()
    db.query(Command).filter(Command.guild_id == guild_id).delete()

    for f in flows_doc.get("flows", []):
        db.add(Flow(
            guild_id=guild_id,
            name=f.get("name"),
            trigger=json.dumps(f.get("trigger")),
            steps=json.dumps(f.get("steps") or f.get("actions")),
            enabled=bool(f.get("enabled", True)),
        ))

    for c in cmds_doc.get("commands", []):
        spec = c.get("spec") or c.get("action")
        db.add(Command(
            guild_id=guild_id,
            name=c.get("name"),
            description=c.get("description"),
            spec=json.dumps(spec),
            enabled=bool(c.get("enabled", True)),
        ))

    db.commit()
    return {"ok": True, "flows": len(flows_doc.get("flows", [])), "commands": len(cmds_doc.get("commands", []))}



# Legacy single-tenant path (kept for compatibility)
FLOWS_PATH = os.getenv("FLOWS_PATH", "flows.json")

# Multi-tenant storage root
BASE_DATA = os.getenv("DATA_DIR", "data")

app.mount("/static", StaticFiles(directory="static", check_dir=False), name="static")
templates = Jinja2Templates(directory="static")


def guild_dir(gid: str) -> str:
    p = os.path.join(BASE_DATA, gid)
    os.makedirs(p, exist_ok=True)
    return p

def read_json(path: str, key: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {key: []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path: str, payload: Dict[str, Any], key: str):
    if key not in payload or not isinstance(payload[key], list):
        raise ValueError(f"Payload must have top-level '{key}' array.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

@app.post("/guilds/{guild_id}/executions")
async def create_execution(
    request: Request,
    guild_id: str,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db)
):
    await require_guild_access(request, guild_id)
    ex = Execution(
        guild_id=guild_id,
        flow_id=payload.get("flow_id"),
        command_id=payload.get("command_id"),
        status=payload.get("status", "queued"),
        input_ctx=json.dumps(payload.get("input_ctx")) if payload.get("input_ctx") is not None else None,
        output_ctx=None,
        error=None,
    )
    db.add(ex)
    db.commit()
    return {"ok": True, "id": ex.id}

@app.post("/executions/{execution_id}/logs")
async def append_log(
    request: Request,
    execution_id: int,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db)
):
    # We trust appenders to only log to their own execution; you can tighten by joining to guild & calling require_guild_access
    lg = Log(
        execution_id=execution_id,
        level=payload.get("level", "INFO"),
        message=str(payload.get("message", "")),
    )
    db.add(lg)
    db.commit()
    return {"ok": True, "log_id": lg.id}

@app.post("/executions/{execution_id}/finish")
async def finish_execution(
    request: Request,
    execution_id: int,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db)
):
    ex = db.query(Execution).filter(Execution.id == execution_id).first()
    if not ex:
        raise HTTPException(404, "Execution not found")
    ex.status = payload.get("status", ex.status)
    if "output_ctx" in payload:
        ex.output_ctx = json.dumps(payload["output_ctx"]) if payload["output_ctx"] is not None else None
    if "error" in payload:
        ex.error = str(payload["error"]) if payload["error"] is not None else None
    ex.finished_at = str(int(time.time()))
    db.commit()
    return {"ok": True}


# ---------- Health ----------
@app.get("/health")
def health():
    return {"ok": True, "version": "0.3.0"}

#------------------------------
@app.post("/preview/fetch")
async def preview_fetch(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    # must be logged in to resolve user/guild-scoped secrets
    sess = require_login(request)
    user_id = sess["user"]["id"]

    method   = (payload.get("method") or "GET").upper()
    url      = payload.get("url") or ""
    headers  = payload.get("headers") or {}
    params   = payload.get("params") or {}
    body     = payload.get("body")
    _gid = payload.get("guild_id")
    guild_id = str(_gid) if _gid not in (None, "") else None

    # inject secrets into all strings (prefer guild scope, then user scope)
    url     = _inject_secrets_in_str(db, user_id, guild_id, url)
    headers = _inject_secrets_in_obj(db, user_id, guild_id, headers)
    params  = _inject_secrets_in_obj(db, user_id, guild_id, params)
    if isinstance(body, str):
        body = _inject_secrets_in_str(db, user_id, guild_id, body)
    else:
        body = _inject_secrets_in_obj(db, user_id, guild_id, body)

    # tolerant timeout parsing
    try:
        timeout_ms = int(payload.get("timeout_ms", 6000))
    except Exception:
        timeout_ms = 6000
    timeout = max(1, min(timeout_ms // 1000, 20))

    # Send request server-side
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=body if isinstance(body, (dict, list)) else None,
                content=None if isinstance(body, (dict, list)) else (body if body else None),
            )

        # Try JSON first; fallback to text
        try:
            j = resp.json()
            return {"ok": True, "status": resp.status_code, "json": j, "text": None}
        except ValueError:
            return {"ok": True, "status": resp.status_code, "json": None, "text": resp.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    
def _smart_preview_format(data, style="auto", row_template="", display_keys=""):
    # lightweight copy of your bot/engine _smart_format (kept here for preview)
    def chunk(s, lim=1900):
        out, cur = [], ""
        for line in str(s).splitlines() or [""]:
            if len(cur) + len(line) + 1 > lim:
                out.append(cur); cur = line
            else:
                cur = (cur + "\n" + line) if cur else line
        if cur: out.append(cur)
        return out

    style = (style or "auto").lower()
    want = [k.strip() for k in (display_keys or "").split(",") if k.strip()]

    # primitives
    if isinstance(data, (str, int, float)) or data is None:
        return chunk(data)

    # list of primitives
    if isinstance(data, list) and (not data or not isinstance(data[0], (dict, list))):
        if style == "json":
            return chunk("```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")
        return chunk("\n".join(f"• {x}" for x in data))

    # list of dicts
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if style == "json":
            return chunk("```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")
        if row_template:
            lines = []
            for r in data:
                try: lines.append(row_template.format(**r))
                except Exception: lines.append(str(r))
            return chunk("\n".join(lines))
        # table-ish (pick a few columns)
        cols = want[:] if want else []
        if not cols:
            # prefer name/id style keys
            pref_name = ["name","title","label","username","town","nation","item"]
            pref_id   = ["uuid","id","code","key"]
            for n in pref_name:
                if any(n in r for r in data):
                    cols.append(n); break
            for i in pref_id:
                if any(i in r for r in data) and i not in cols:
                    cols.append(i)
            if not cols:
                seen = []
                for r in data:
                    for k in r.keys():
                        if k not in seen:
                            seen.append(k)
                        if len(seen) >= 3: break
                    if len(seen) >= 3: break
                cols = seen[:3]
        if style == "table" or (style == "auto" and len(cols) >= 2):
            # simple monospace table
            srows = [{c: "" if r.get(c) is None else str(r.get(c)) for c in cols} for r in data]
            w = {c: max(len(c), *(len(sr[c]) for sr in srows)) for c in cols}
            header = " | ".join(c.ljust(w[c]) for c in cols)
            sep    = "-+-".join("-"*w[c] for c in cols)
            lines  = [header, sep] + [" | ".join(r[c].ljust(w[c]) for c in cols) for r in srows]
            return chunk("```\n" + "\n".join(lines) + "\n```")
        key = cols[0] if cols else None
        return chunk("\n".join(f"• {r.get(key)}" if key else f"• {r}" for r in data))

    # dict
    if isinstance(data, dict):
        if style == "json":
            return chunk("```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")
        return chunk("\n".join(f"• {k}: {v}" for k, v in data.items()))

    # fallback
    return chunk("```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")


@app.post("/preview/steps")
async def preview_steps(payload: dict = Body(...)):
    """
    Dry-run a subset of 'steps': http, extract, set, format.
    Returns {'ok': True, 'chunks': [str, ...]} for the dock to render.
    """
    steps = payload.get("steps") or []
    ctx = {}
    data_for_format = None
    fmt = {"style": "auto", "row_tmpl": "", "display_keys": ""}

    for st in steps:
        kind = (st.get("kind") or st.get("type") or "").lower()

        if kind == "http":
            method = (st.get("method") or "GET").upper()
            url    = st.get("url") or ""
            headers= st.get("headers") or {}
            params = st.get("params") or {}
            body   = st.get("json") if isinstance(st.get("json"), (dict, list)) else None
            timeout= max(1, min(int(st.get("timeout", 10)), 20))
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.request(method, url, headers=headers, params=params, json=body)
                try:
                    j = r.json()
                    ctx[st.get("save_as","data")] = j
                except ValueError:
                    ctx[st.get("save_as","data")] = {"text": r.text, "status": r.status_code}
            except Exception as e:
                return {"ok": False, "error": f"HTTP error: {e}"}

        elif kind == "extract":
            source = st.get("from", "data")
            expr   = st.get("expr") or st.get("path") or ""  # allow 'path' for older UIs
            src    = ctx.get(source)
            try:
                ctx[st.get("save_as","rows")] = jmespath.search(expr, src) if src is not None else None
            except Exception as e:
                return {"ok": False, "error": f"JMESPath error: {e}"}

        elif kind == "set":
            key, val = st.get("key"), st.get("value")
            if isinstance(val, str):
                # simple token replace with ctx values
                try: val = val.format_map({**{k:v for k,v in ctx.items()}})
                except Exception: pass
            if key: ctx[key] = val

        elif kind == "format":
            data_for_format = ctx.get(st.get("from","rows"))
            fmt["style"] = st.get("style","auto")
            fmt["row_tmpl"] = st.get("row_tmpl") or st.get("row_template") or ""
            fmt["display_keys"] = st.get("keys") or st.get("display_keys") or ""

        elif kind == "send":
            # stop at send: preview only needs to render text/embed that would be sent
            break

    # Pick a default data source if format not specified
    if data_for_format is None:
        data_for_format = ctx.get("rows", ctx.get("data"))

    chunks = _smart_preview_format(
        data_for_format,
        style=fmt["style"],
        row_template=fmt["row_tmpl"],
        display_keys=fmt["display_keys"]
    )
    return {"ok": True, "chunks": chunks}    

# ---------- Multi-guild: flows ----------
@app.get("/guilds/{guild_id}/flows")
async def get_flows(guild_id: str, request: Request, db: Session = Depends(get_db)):
    if not _bot_ok(request):
        await require_guild_access(request, guild_id)
    rows = db.query(Flow).filter(Flow.guild_id == guild_id).all()
    out = []
    for f in rows:
        try: trigger = json.loads(f.trigger) if f.trigger else None
        except Exception: trigger = f.trigger
        try: steps = json.loads(f.steps) if f.steps else None
        except Exception: steps = f.steps
        out.append(dict(id=f.id, name=f.name, trigger=trigger, steps=steps, enabled=f.enabled))
    return {"flows": out}

@app.post("/guilds/{guild_id}/flows")
async def save_flows(guild_id: str, request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    await require_guild_access(request, guild_id)
    db.query(Flow).filter(Flow.guild_id == guild_id).delete()
    for f in payload.get("flows", []):
        db.add(Flow(
            guild_id=guild_id,
            name=f.get("name"),
            trigger=json.dumps(f.get("trigger")),
            steps=json.dumps(f.get("steps") or f.get("actions")),
            enabled=bool(f.get("enabled", True)),
        ))
    db.commit()
    # trigger bot hot-reload
    await notify_bot(guild_id, "flows")
    return {"ok": True}



# --- Persist ON/OFF state for flows without triggering a reload ---
@app.patch("/guilds/{guild_id}/flows/enabled")
async def patch_flows_enabled(
    guild_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db)
):
    if not _bot_ok(request):
        await require_guild_access(request, guild_id)

    updates = body.get("updates") or []
    if not updates and "id" in body:
        updates = [{"id": body["id"], "enabled": bool(body.get("enabled", True))}]
    if not isinstance(updates, list):
        raise HTTPException(status_code=400, detail="Provide 'id'+'enabled' or 'updates': [...]")

    changed = 0
    for u in updates:
        fid = u.get("id")
        if fid is None:
            continue
        row = db.query(Flow).filter(Flow.guild_id == guild_id, Flow.id == fid).first()
        if row:
            row.enabled = bool(u.get("enabled", True))
            changed += 1
    db.commit()
    return {"ok": True, "changed": changed}


# ---------- Multi-guild: commands ----------
@app.get("/guilds/{guild_id}/commands")
async def get_commands(guild_id: str, request: Request, db: Session = Depends(get_db)):
    if not _bot_ok(request):
        await require_guild_access(request, guild_id)
    rows = db.query(Command).filter(Command.guild_id == guild_id).all()
    out = []
    for c in rows:
        try: spec = json.loads(c.spec) if c.spec else None
        except Exception: spec = c.spec
        out.append(dict(id=c.id, name=c.name, description=c.description, spec=spec, enabled=c.enabled))
    return {"commands": out}

@app.post("/guilds/{guild_id}/commands")
async def save_commands(guild_id: str, request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    await require_guild_access(request, guild_id)
    db.query(Command).filter(Command.guild_id == guild_id).delete()
    for c in payload.get("commands", []):
        spec = c.get("spec") or c.get("action")
        db.add(Command(
            guild_id=guild_id,
            name=c.get("name"),
            description=c.get("description"),
            spec=json.dumps(spec),
            enabled=bool(c.get("enabled", True)),
        ))
    db.commit()
    # trigger bot hot-reload
    await notify_bot(guild_id, "commands")
    return {"ok": True}


# ---------- UI ----------


# don't fail if there's no ./static directory yet
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")

@app.get("/", response_class=HTMLResponse)
def ui():
    # prefer root index.html (your repo layout), fallback to static/index.html
    path = INDEX_FILE if INDEX_FILE.exists() else (STATIC_DIR / "index.html")
    return FileResponse(str(path))