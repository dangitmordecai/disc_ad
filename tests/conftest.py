"""
Shared pytest fixtures.

Key responsibilities:
- Set required env vars before any production module is imported.
- For API tests: patch SQLAlchemy to use a StaticPool so all sessions
  share the same in-memory SQLite database and see the tables created at
  main.py import time.
"""
import os
import sys

# Must be set before any production module imports
_ENV = {
    "SECRETS_ENC_KEY": "YAwq8JSur0EEfuZs_dsSvT-lYFLXuVMLi8GRmpvIUvQ=",
    "SECRET_MASTER_KEY": "masterkey",
    "DATABASE_URL": "sqlite:///:memory:",
    "SESSION_SECRET": "test-session-secret",
    "DISCORD_BOT_TOKEN": "fake-bot-token",
    "DISCORD_CLIENT_ID": "fake-client-id",
    "DISCORD_CLIENT_SECRET": "fake-client-secret",
    "DISCORD_REDIRECT_URI": "http://localhost/callback",
}
for k, v in _ENV.items():
    os.environ.setdefault(k, v)
