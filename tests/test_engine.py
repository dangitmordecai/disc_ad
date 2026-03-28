"""
Tests for engine.py — covers the bugs fixed in this session and core logic.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so engine.py can be imported without a real Discord token
# or database connection.
# ---------------------------------------------------------------------------
import sys
import types

# Stub discord module
discord_stub = types.ModuleType("discord")
discord_stub.Client = object
discord_stub.Member = object
discord_stub.Message = object
discord_stub.Guild = object
discord_stub.TextChannel = object
discord_stub.Thread = object
discord_stub.Embed = MagicMock(return_value=MagicMock())
discord_stub.Color = MagicMock()
discord_stub.Color.blurple = MagicMock(return_value=0x5865F2)
discord_stub.abc = types.ModuleType("discord.abc")
discord_stub.abc.GuildChannel = object
discord_stub.RawReactionActionEvent = object
discord_stub.AppCommandType = MagicMock()
discord_stub.Object = MagicMock
discord_stub.SelectOption = MagicMock
discord_stub.ButtonStyle = MagicMock()
discord_stub.ButtonStyle.primary = 1
discord_stub.ButtonStyle.secondary = 2
discord_stub.ButtonStyle.danger = 3
discord_stub.ui = types.ModuleType("discord.ui")
discord_stub.ui.View = object
discord_stub.ui.Button = object
discord_stub.ui.Select = object
discord_stub.app_commands = types.ModuleType("discord.app_commands")
discord_stub.app_commands.CommandTree = MagicMock
discord_stub.utils = MagicMock()
discord_stub.PermissionOverwrite = MagicMock
discord_stub.File = MagicMock
sys.modules["discord"] = discord_stub
sys.modules["discord.abc"] = discord_stub.abc
sys.modules["discord.ui"] = discord_stub.ui
sys.modules["discord.app_commands"] = discord_stub.app_commands

# Stub aiohttp
aiohttp_stub = types.ModuleType("aiohttp")
aiohttp_stub.ClientTimeout = MagicMock(return_value=MagicMock())
aiohttp_stub.ClientSession = MagicMock
sys.modules["aiohttp"] = aiohttp_stub

# Stub config / crypto / main imports that engine.py pulls
config_stub = types.ModuleType("config")
config_stub.get_settings = MagicMock(return_value=MagicMock(database_url="sqlite:///:memory:"))
sys.modules["config"] = config_stub

crypto_stub = types.ModuleType("crypto")
crypto_stub.encrypt_for_user = MagicMock(return_value="enc")
crypto_stub.decrypt_for_user = MagicMock(return_value="plain")
sys.modules["crypto"] = crypto_stub

# Stub sqlalchemy bits that engine imports via main
import importlib, os
os.environ.setdefault("SECRETS_ENC_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleTA=")
os.environ.setdefault("SECRET_MASTER_KEY", "masterkey")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Provide a minimal 'main' stub so engine can import from it
main_stub = types.ModuleType("main")
main_stub.SessionLocal = MagicMock()
main_stub.Flow = MagicMock()
main_stub.Command = MagicMock()
main_stub.Secret = MagicMock()
main_stub.decrypt_value = lambda x: x
sys.modules["main"] = main_stub

# Now import the real engine
import engine
from engine import (
    Ctx,
    format_template,
    run_steps,
    _chunk_text,
    _smart_format,
    _dot_get,
    FlowEngine,
)


# ---------------------------------------------------------------------------
# format_template
# ---------------------------------------------------------------------------

class TestFormatTemplate:
    def test_simple_substitution(self):
        assert format_template("Hello {name}", {"name": "world"}) == "Hello world"

    def test_dotted_key(self):
        assert format_template("{user.name}", {"user": {"name": "Alice"}}) == "Alice"

    def test_missing_key_becomes_empty(self):
        assert format_template("{missing}", {}) == ""

    def test_none_template(self):
        assert format_template(None, {}) == ""

    def test_nested_missing(self):
        assert format_template("{a.b.c}", {"a": {"b": {}}}) == ""


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_short_text_single_chunk(self):
        chunks = _chunk_text("hello world")
        assert chunks == ["hello world"]

    def test_splits_at_limit(self):
        line = "x" * 950
        text = "\n".join([line, line, line])
        chunks = _chunk_text(text, limit=1900)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c) <= 1900

    def test_empty_string(self):
        assert _chunk_text("") == []


# ---------------------------------------------------------------------------
# _dot_get
# ---------------------------------------------------------------------------

class TestDotGet:
    def test_simple_key(self):
        assert _dot_get({"a": 1}, "a") == 1

    def test_nested(self):
        assert _dot_get({"a": {"b": 2}}, "a.b") == 2

    def test_list_index(self):
        assert _dot_get({"items": [10, 20]}, "items[1]") == 20

    def test_missing_returns_none(self):
        assert _dot_get({"a": 1}, "b") is None

    def test_empty_path(self):
        obj = {"x": 1}
        assert _dot_get(obj, "") is obj


# ---------------------------------------------------------------------------
# run_steps — regression: must NOT raise ValueError for handled step types
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_steps_set():
    ctx = Ctx()
    await run_steps(ctx, [{"type": "set", "key": "foo", "value": "bar"}])
    assert ctx["foo"] == "bar"


@pytest.mark.asyncio
async def test_run_steps_extract():
    ctx = Ctx(data={"items": [1, 2, 3]})
    await run_steps(ctx, [{"type": "extract", "from": "data", "expr": "items", "save_as": "rows"}])
    assert ctx["rows"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_run_steps_format():
    ctx = Ctx(rows=["a", "b"])
    await run_steps(ctx, [{"type": "format", "from": "rows", "style": "auto", "save_as": "text"}])
    assert "a" in ctx["text"]
    assert "b" in ctx["text"]


@pytest.mark.asyncio
async def test_run_steps_branch_then():
    ctx = Ctx(x=5)
    await run_steps(ctx, [{
        "type": "branch",
        "when": "x",
        "then": [{"type": "set", "key": "result", "value": "yes"}],
        "else": [{"type": "set", "key": "result", "value": "no"}],
    }])
    assert ctx["result"] == "yes"


@pytest.mark.asyncio
async def test_run_steps_branch_else():
    ctx = Ctx(x=None)
    await run_steps(ctx, [{
        "type": "branch",
        "when": "x",
        "then": [{"type": "set", "key": "result", "value": "yes"}],
        "else": [{"type": "set", "key": "result", "value": "no"}],
    }])
    assert ctx["result"] == "no"


@pytest.mark.asyncio
async def test_run_steps_if_jmespath():
    ctx = Ctx(score=10)
    await run_steps(ctx, [{
        "type": "if",
        "expr": "score",
        "then": [{"type": "set", "key": "label", "value": "has_score"}],
        "else": [{"type": "set", "key": "label", "value": "no_score"}],
    }])
    assert ctx["label"] == "has_score"


@pytest.mark.asyncio
async def test_run_steps_foreach():
    ctx = Ctx(items=[1, 2, 3], total=0)
    await run_steps(ctx, [{
        "type": "foreach",
        "items": "items",
        "as": "n",
        "steps": [{"type": "set", "key": "last", "value": "{n}"}],
    }])
    assert ctx["last"] == "3"


@pytest.mark.asyncio
async def test_run_steps_unknown_type_silently_skipped():
    ctx = Ctx()
    # Should not raise
    await run_steps(ctx, [{"type": "definitely_unknown_step_xyz"}])


@pytest.mark.asyncio
async def test_run_steps_loop():
    ctx = Ctx(rows=[{"v": 1}, {"v": 2}])
    await run_steps(ctx, [{
        "type": "loop",
        "from": "rows",
        "save_each_as": "out",
        "do": [{"type": "set", "key": "out", "value": "hit"}],
    }])
    # save_each_as collects ctx2["out"] after each iteration
    assert ctx["out"] == ["hit", "hit"]


# ---------------------------------------------------------------------------
# FlowEngine.__init__ — _tasks and _disabled_ids must exist
# ---------------------------------------------------------------------------

class TestFlowEngineInit:
    def _make_engine(self, flows=None):
        bot = MagicMock()
        bot.get_guild = MagicMock(return_value=None)
        return FlowEngine(bot=bot, flows=flows or [], guild_id=123)

    def test_tasks_initialized(self):
        eng = self._make_engine()
        assert hasattr(eng, "_tasks")
        assert isinstance(eng._tasks, list)

    def test_disabled_ids_initialized(self):
        eng = self._make_engine()
        assert hasattr(eng, "_disabled_ids")
        assert isinstance(eng._disabled_ids, set)

    def test_set_flows_list(self):
        eng = self._make_engine(flows=[{"id": 1, "name": "f1"}])
        assert len(eng.flows) == 1

    def test_set_flows_dict(self):
        eng = self._make_engine(flows={"flows": [{"id": 1}]})
        assert len(eng.flows) == 1

    def test_is_enabled_default_true(self):
        eng = self._make_engine()
        assert eng._is_enabled({"id": "1", "enabled": True})

    def test_is_enabled_false_when_disabled(self):
        eng = self._make_engine()
        eng._disabled_ids.add("1")
        assert not eng._is_enabled({"id": "1"})

    def test_toggle_flow(self):
        eng = self._make_engine(flows=[{"id": "42", "name": "test", "enabled": True}])
        result = eng.toggle_flow("42")
        assert result is False  # toggled OFF
        result2 = eng.toggle_flow("42")
        assert result2 is True  # toggled ON


# ---------------------------------------------------------------------------
# handle_message_regex — no infinite recursion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_message_regex_no_infinite_recursion():
    """
    Regression: previously called self.handle_message_regex(message) recursively.
    After the fix the coroutine must complete without hitting recursion limit.
    """
    bot = MagicMock()
    guild = MagicMock()
    guild.id = 123
    bot.get_guild = MagicMock(return_value=guild)

    eng = FlowEngine(bot=bot, flows=[], guild_id=123)
    # One flow with a matching trigger
    eng.flows = [{
        "id": "1",
        "name": "test",
        "enabled": True,
        "trigger": {"type": "message_regex", "pattern": "hello"},
        "steps": [],
    }]

    # Patch _execute_flow so we don't need a real Discord environment
    eng._execute_flow = AsyncMock()

    message = MagicMock()
    message.guild = guild
    message.guild.id = 123
    message.author.bot = False
    message.content = "hello world"
    message.channel.id = 456
    message.id = 789
    message.author.id = 111
    message.author.mention = "<@111>"

    # This should complete; if recursion bug is present it raises RecursionError
    await eng.handle_message_regex(message)
    assert eng._execute_flow.call_count == 1
