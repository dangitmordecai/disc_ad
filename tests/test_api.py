"""
Tests for main.py API — covers routing, auth guards, and the secret endpoints.
Uses FastAPI's TestClient (synchronous) so no asyncio fixture needed.
"""
import os
import sys
import types
import pytest

# ---------------------------------------------------------------------------
# Environment vars that main.py validates at import time
# ---------------------------------------------------------------------------
os.environ["SECRETS_ENC_KEY"] = "YAwq8JSur0EEfuZs_dsSvT-lYFLXuVMLi8GRmpvIUvQ="
os.environ["SECRET_MASTER_KEY"] = "masterkey"
os.environ["DATABASE_URL"] = "sqlite:///./test_disc_ad_tmp.db"
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["DISCORD_CLIENT_ID"] = "fake-client-id"
os.environ["DISCORD_CLIENT_SECRET"] = "fake-client-secret"
os.environ["DISCORD_REDIRECT_URI"] = "http://localhost/callback"
os.environ["DISCORD_BOT_TOKEN"] = "fake-bot-token"

# Clear any stubs that engine tests may have left in the module cache
for _mod in list(sys.modules.keys()):
    if _mod in ("main", "engine", "config", "crypto"):
        sys.modules.pop(_mod, None)

# Stub slowapi before main.py imports it
slowapi_stub = types.ModuleType("slowapi")
slowapi_stub.Limiter = MagicMock = type("Limiter", (), {
    "__init__": lambda self, **kw: None,
    "limit": lambda self, *a, **kw: (lambda f: f),
})
slowapi_stub._rate_limit_exceeded_handler = lambda *a, **kw: None
slowapi_errors = types.ModuleType("slowapi.errors")
class _RLE(Exception): pass
slowapi_errors.RateLimitExceeded = _RLE
slowapi_util = types.ModuleType("slowapi.util")
slowapi_util.get_remote_address = lambda r: "127.0.0.1"
sys.modules["slowapi"] = slowapi_stub
sys.modules["slowapi.errors"] = slowapi_errors
sys.modules["slowapi.util"] = slowapi_util

from fastapi.testclient import TestClient
import main as m

client = TestClient(m.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Auth guards — unauthenticated requests must return 401
# ---------------------------------------------------------------------------

def test_me_unauthenticated():
    r = client.get("/me")
    assert r.status_code == 401


def test_list_secrets_unauthenticated():
    r = client.get("/secrets")
    assert r.status_code == 401


def test_flows_unauthenticated():
    r = client.get("/guilds/123/flows")
    assert r.status_code in (401, 403)


def test_commands_unauthenticated():
    r = client.get("/guilds/123/commands")
    assert r.status_code in (401, 403)


def test_executions_unauthenticated():
    r = client.post("/guilds/123/executions", json={})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Bot-secret bypass (internal bot requests)
# ---------------------------------------------------------------------------

def test_flows_with_bot_secret():
    os.environ["DASHBOARD_SECRET"] = "super-secret"
    try:
        r = client.get(
            "/guilds/123/flows",
            headers={"X-DASHBOARD-SECRET": "super-secret"},
        )
        assert r.status_code == 200
        assert "flows" in r.json()
    finally:
        del os.environ["DASHBOARD_SECRET"]


def test_flows_with_wrong_bot_secret():
    os.environ["DASHBOARD_SECRET"] = "super-secret"
    try:
        r = client.get(
            "/guilds/123/flows",
            headers={"X-DASHBOARD-SECRET": "wrong"},
        )
        assert r.status_code in (401, 403)
    finally:
        del os.environ["DASHBOARD_SECRET"]


# ---------------------------------------------------------------------------
# OAuth redirect — must redirect to Discord
# ---------------------------------------------------------------------------

def test_login_redirect():
    r = client.get("/auth/discord/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers.get("location", "")
    assert "discord.com" in loc


# ---------------------------------------------------------------------------
# upsert_secret — enc_value bug regression
# Posting without auth must fail; we verify the endpoint exists and rejects
# unauthenticated callers rather than crashing on missing enc_value.
# ---------------------------------------------------------------------------

def test_upsert_secret_unauthenticated():
    r = client.post("/secrets", json={"key": "FOO", "value": "bar"})
    assert r.status_code == 401


def test_delete_secret_unauthenticated():
    r = client.delete("/secrets/1")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# preview/steps — dry-run endpoint (no auth required)
# ---------------------------------------------------------------------------

def test_preview_steps_set():
    r = client.post("/preview/steps", json={"steps": [
        {"kind": "set", "key": "greeting", "value": "hello"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_preview_steps_extract():
    r = client.post("/preview/steps", json={"steps": [
        {"kind": "set", "key": "data", "value": {"items": [1, 2, 3]}},
        {"kind": "extract", "from": "data", "expr": "items", "save_as": "rows"},
        {"kind": "format", "from": "rows", "style": "auto"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
