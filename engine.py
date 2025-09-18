import asyncio
import datetime
from typing import Dict, Any, Optional, List
import aiohttp, json, re
import jmespath
import discord
from discord import Message
import re, json, datetime
from sqlalchemy import create_engine, text  # add
from config import get_settings            # add
from main import decrypt_value             # add (you already have it in main.py)

import io

# engine.py (if running inside the API service)
from sqlalchemy.orm import Session
from main import SessionLocal, Flow, Command, Secret, decrypt_value  # or move models/helpers to a shared module


# --- secret injection helpers (runtime) ---
SECRET_TAG = re.compile(r"\{\{\s*secret\.([A-Za-z0-9_]+)\s*\}\}")

_settings = get_settings()
_db_engine = create_engine(_settings.database_url, future=True)

def _resolve_secret_for_guild(key: str, guild_id: str | None) -> str | None:
    with _db_engine.connect() as conn:
        # 1) try guild-scoped override
        if guild_id:
            row = conn.execute(
                text("SELECT value_enc FROM secrets WHERE key=:k AND guild_id=:g ORDER BY id DESC LIMIT 1"),
                {"k": key, "g": str(guild_id)},
            ).fetchone()
            if row:
                return decrypt_value(row[0])
        # 2) fallback to user-wide (guild_id is NULL)
        row = conn.execute(
            text("SELECT value_enc FROM secrets WHERE key=:k AND guild_id IS NULL ORDER BY id DESC LIMIT 1"),
            {"k": key},
        ).fetchone()
        return decrypt_value(row[0]) if row else None

def _inject_secrets_in_obj(obj, guild_id: str | None):
    def walk(x):
        if isinstance(x, str):
            def repl(m: re.Match):
                v = _resolve_secret_for_guild(m.group(1), guild_id)
                return v if v is not None else m.group(0)
            return SECRET_TAG.sub(repl, x)
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    return walk(obj)
# --- end helpers ---



def load_flows_from_db(guild_id: str):
    with SessionLocal() as db:
        rows = db.query(Flow).filter(Flow.guild_id == guild_id).all()
        out = []
        for f in rows:
            try: trig = json.loads(f.trigger) if f.trigger else None
            except Exception: trig = f.trigger
            try: steps = json.loads(f.steps) if f.steps else None
            except Exception: steps = f.steps
            out.append({"id": f.id, "name": f.name, "trigger": trig, "steps": steps, "enabled": f.enabled})
        return out

def load_commands_from_db(guild_id: str):
    with SessionLocal() as db:
        rows = db.query(Command).filter(Command.guild_id == guild_id).all()
        out = []
        for c in rows:
            try: spec = json.loads(c.spec) if c.spec else None
            except Exception: spec = c.spec
            out.append({"id": c.id, "name": c.name, "description": c.description, "spec": spec, "enabled": c.enabled})
        return out

def resolve_secret(user_id: str, guild_id: str | None, key: str) -> str | None:
    with SessionLocal() as db:
        # guild override
        if guild_id:
            s = db.query(Secret).filter(Secret.user_id==user_id, Secret.guild_id==guild_id, Secret.key==key).first()
            if s: return decrypt_value(s.value_enc)
        # fallback to user-global
        s = db.query(Secret).filter(Secret.user_id==user_id, Secret.guild_id==None, Secret.key==key).first()
        return decrypt_value(s.value_enc) if s else None



class SafeDict(dict):
    """Safe dict for str.format_map that leaves {missing} tokens untouched."""
    def __missing__(self, key):
        return "{" + key + "}"

def _parse_json_obj(maybe):
    if isinstance(maybe, dict):
        return maybe
    if not maybe:
        return {}
    try:
        return json.loads(maybe)
    except Exception:
        return {}

_DOT_PARTS = re.compile(r'([^[.\]]+)|\[(\d+)\]')

def _dot_get(obj, path: str):
    if not path:
        return obj
    cur = obj
    try:
        for m in _DOT_PARTS.finditer(path):
            key, idx = m.group(1), m.group(2)
            if key is not None:
                cur = cur[key]
            else:
                cur = cur[int(idx)]
        return cur
    except Exception:
        return None

def _chunk_text(s: str, limit: int = 1900) -> List[str]:
    out, cur = [], ""
    for line in s.splitlines() or [""]:
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out

_PREF_NAME_KEYS = ["name","title","label","username","town","nation","item"]
_PREF_ID_KEYS   = ["uuid","id","code","key"]

def _pick_keys(rows: List[Dict[str, Any]], want: List[str]|None, max_cols=3) -> List[str]:
    if want:
        return [k for k in want if any(k in r for r in rows)][:max_cols]
    # try name + id
    for nk in _PREF_NAME_KEYS:
        if any(nk in r for r in rows):
            for ik in _PREF_ID_KEYS:
                if any(ik in r for r in rows):
                    return [nk, ik][:max_cols]
            return [nk]
    # else first few keys seen
    seen = []
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.append(k)
            if len(seen) >= max_cols:
                return seen[:max_cols]
    return seen[:max_cols]

def _as_table(rows: List[Dict[str, Any]], cols: List[str]) -> str:
    # stringify + width calc
    srows = [{c: ("" if r.get(c) is None else str(r.get(c))) for c in cols} for r in rows]
    widths = {c: max(len(c), *(len(sr[c]) for sr in srows)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep    = "-+-".join("-"*widths[c] for c in cols)
    lines  = [header, sep]
    for r in srows:
        lines.append(" | ".join(r[c].ljust(widths[c]) for c in cols))
    return "```\n" + "\n".join(lines) + "\n```"

def _smart_format(data: Any, action: dict, ctx: dict) -> List[str]:
    """
    Returns chunks of formatted text under ~1900 chars each,
    based on action.render_style / display_keys / row_template.
    """
    style = (action.get("render_style") or "auto").lower()
    row_tmpl = action.get("row_template") or ""
    want_keys = [k.strip() for k in (action.get("display_keys") or "").split(",") if k.strip()]

    # 1) If JSON string already
    if isinstance(data, (str, int, float)) or data is None:
        return _chunk_text(str(data))

    # 2) List of primitives -> bullets
    if isinstance(data, list) and (not data or not isinstance(data[0], (dict, list))):
        if style in ("json",) :
            return _chunk_text("```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")
        bullets = "\n".join(f"• {str(x)}" for x in data)
        return _chunk_text(bullets)

    # 3) List of dicts
    if isinstance(data, list) and data and isinstance(data[0], dict):
        rows = data
        if style == "json":
            return _chunk_text("```json\n" + json.dumps(rows, ensure_ascii=False, indent=2) + "\n```")
        # row template
        if row_tmpl:
            lines = []
            for r in rows:
                try:
                    lines.append(row_tmpl.format(**r))
                except Exception:
                    lines.append(str(r))
            return _chunk_text("\n".join(lines))
        # table/list auto
        cols = _pick_keys(rows, want_keys, max_cols=3)
        if style == "table" or (style == "auto" and len(cols) >= 2):
            return _chunk_text(_as_table(rows, cols))
        # fallback to bullets with first key
        key = cols[0] if cols else None
        lines = [f"• {r.get(key)}" if key else f"• {r}" for r in rows]
        return _chunk_text("\n".join(lines))

    # 4) Dict -> key: value list
    if isinstance(data, dict):
        if style == "json":
            return _chunk_text("```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")
        lines = [f"• {k}: {v}" for k,v in data.items()]
        return _chunk_text("\n".join(lines))

    # 5) Fallback pretty JSON
    return _chunk_text("```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")

# Put near your other small helpers in engine.py
import re

_DOT_FIELD = re.compile(r"{\s*([^{}]+?)\s*}")  # captures the stuff inside {...}

def format_template(tmpl: str, ctx: dict) -> str:
    """
    Safe formatter:
    - Supports dotted keys like {user.name} but ONLY through dicts.
    - Unknown paths become "" (empty) to avoid AttributeError.
    - Never touches object attributes, so it's safe with things like FlowEngine.
    """
    if tmpl is None:
        return ""
    s = str(tmpl)

    def resolve(field: str):
        parts = field.split(".")
        cur = ctx
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                # Unknown -> empty string (or return "{" + field + "}" to keep it visible)
                return ""
        return "" if cur is None else str(cur)

    return _DOT_FIELD.sub(lambda m: resolve(m.group(1)), s)


def parse_channel_ref(ref: str) -> Optional[int]:
    """Accepts an integer ID (as str), or '#name' / 'name' and returns an int if it's an ID."""
    if not ref:
        return None
    ref = ref.strip()
    if ref.startswith("#"):
        ref = ref[1:]
    return int(ref) if ref.isdigit() else None


async def find_channel(guild: discord.Guild, channel_ref: str) -> Optional[discord.TextChannel]:
    """
    Resolve a channel by ID or name within a guild.
    Returns a TextChannel or None.
    """
    cid = parse_channel_ref(channel_ref)
    if cid:
        ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
        if isinstance(ch, discord.TextChannel):
            return ch

    # fallback: match by name (case-insensitive)
    name = (channel_ref or "").lstrip("#").lower().strip()
    for ch in guild.text_channels:
        if ch.name.lower() == name:
            return ch
    return None

class Ctx(dict):
    """Context that supports dot/attr-like access in templates via SafeDict already in your code."""
    pass

async def _http_step(ctx: Ctx, step: Dict[str, Any]):
    url = format_template(step["url"], ctx)
    method = step.get("method", "GET").upper()
    headers = {k: format_template(v, ctx) for k, v in step.get("headers", {}).items()}
    params = {k: format_template(v, ctx) for k, v in step.get("params", {}).items()}
    json_body = step.get("json")
    if isinstance(json_body, (dict, list, str)):
        # allow templating inside JSON by dumping -> formatting -> loading
        jb = json.dumps(json_body)
        jb = format_template(jb, ctx)
        json_body = json.loads(jb)

    timeout = aiohttp.ClientTimeout(total=float(step.get("timeout", 10)))
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for attempt in range(int(step.get("retries", 0)) + 1):
            try:
                async with sess.request(method, url, headers=headers, params=params, json=json_body) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    ctx[step.get("save_as", "data")] = data
                    return
            except Exception as e:
                if attempt >= int(step.get("retries", 0)):
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))

def _extract_step(ctx: Ctx, step: Dict[str, Any]):
    source = step.get("from", "data")
    expr = step.get("expr") or step.get("path") or "@"  # accept UI shape
    data = ctx.get(source)
    if data is None:
        ctx[step.get("save_as", "rows")] = None
        return
    res = jmespath.search(expr, data)
    ctx[step.get("save_as", "rows")] = res


def _set_step(ctx: Ctx, step: Dict[str, Any]):
    # Simple templated set
    key = step["key"]
    value = step["value"]
    if isinstance(value, str):
        value = format_template(value, ctx)
    ctx[key] = value

async def _branch_step(ctx: Ctx, step: Dict[str, Any], run_steps):
    cond_expr = step["when"]
    ok = jmespath.search(cond_expr, dict(ctx))
    if ok:
        await run_steps(ctx, step.get("then", []))
    else:
        await run_steps(ctx, step.get("else", []))

async def _loop_step(ctx: Ctx, step: Dict[str, Any], run_steps):
    source = step.get("from", "rows")
    arr = ctx.get(source) or []
    save = step.get("save_each_as")  # optional: collect outputs
    collected = []
    for i, item in enumerate(arr):
        ctx2 = Ctx(ctx)
        ctx2["item"] = item
        ctx2["i"] = i
        await run_steps(ctx2, step.get("do", []))
        if save:
            collected.append(ctx2.get(save))
    if save:
        ctx[save] = collected

def _format_step(ctx: Ctx, step: Dict[str, Any]):
    data = ctx.get(step.get("from", "rows"))
    style = step.get("style", "auto")
    row_tmpl = step.get("row_tmpl") or step.get("row_template")
    want_keys = step.get("keys") or step.get("display_keys")

    chunks = _smart_format(
        data,
        {
            "render_style": style,
            "row_template": row_tmpl,
            "display_keys": want_keys,
        },
        ctx,
    )
    ctx[step.get("save_as", "text")] = "\n".join(chunks)

async def _send_step(ctx: Ctx, step: Dict[str, Any], discord_client):
    channel_id = step.get("channel_id") or ctx.get("channel_id")
    if not channel_id:
        # allow templated channel name/ID
        channel_id = format_template(step.get("channel", "") or (ctx.get("channel") or ""), ctx)
        try:
            channel_id = int(channel_id)
        except Exception:
            pass

    channel = discord_client.get_channel(int(channel_id)) if channel_id else None
    if not channel:
        return



    mode = step.get("mode", "message")
    paginate = step.get("paginate", False)
    content_var = step.get("content_var", "text")
    payload = ctx.get(content_var, "")

    if mode == "embed":
        title = format_template(step.get("embed_title", "Result"), ctx)
        embed = discord.Embed(title=title, description=payload[:4096], color=discord.Color.blurple())
        view = None
        if paginate and isinstance(payload, str) and len(payload) > 1900:
            # basic paginator: split into 1800-char chunks and attach Next/Prev buttons
            pages = [payload[i:i+1800] for i in range(0, len(payload), 1800)]
            embed.description = pages[0]
            view = make_paginator_view(embed, pages)  # implement with discord.ui.View
        await channel.send(embed=embed, view=view)
    else:
        if paginate and isinstance(payload, str) and len(payload) > 1900:
            pages = [payload[i:i+1900] for i in range(0, len(payload), 1900)]
            msg = await channel.send(pages[0], view=make_paginator_view(None, pages))
            return
        await channel.send(payload)

async def run_steps(ctx: Ctx, steps: List[Dict[str, Any]], discord_client=None):
    for step in steps:
        t = (step.get("type") or step.get("kind") or "").lower()  # accept UI shape
        if t == "http":
            await _http_step(ctx, step)
        elif t == "extract":
            _extract_step(ctx, step)
        elif t == "set":
            _set_step(ctx, step)
        elif t == "branch":
            await _branch_step(ctx, step, lambda c,s: run_steps(c,s,discord_client))
        elif t == "loop":
            await _loop_step(ctx, step, lambda c,s: run_steps(c,s,discord_client))
        elif t == "format":
            _format_step(ctx, step)
        elif t == "send":
            if discord_client:
                await _send_step(ctx, step, discord_client)
        if t == "set":
            ctx[step["key"]] = step.get("value")
            continue

        if t == "if":
            expr = step.get("expr") or step.get("condition") or ""
            def _safe_eval(e, c):
                return eval(e, {"__builtins__": {}}, {"ctx": c, "len": len, "int": int, "str": str, "float": float})
            branch = step.get("then", []) if _safe_eval(expr, ctx) else step.get("else", [])
            await run_steps(ctx, branch, discord_client=discord_client)
            continue

        if t in {"for_each", "foreach"}:
            items_ref = step.get("items")
            varname = step.get("as") or "item"
            items = ctx.get(items_ref) if isinstance(items_ref, str) else items_ref
            if isinstance(items, list):
                for it in items:
                    ctx[varname] = it
                    await run_steps(ctx, step.get("steps", []), discord_client=discord_client)
            continue        
        else:
            raise ValueError(f"Unknown step type: {t}")


class FlowEngine:
    """
    Executes declarative 'flows' against one or more guilds.

    Supported triggers:
      - {"type": "schedule", "interval_seconds": int}
      - {"type": "member_join"}
      - {"type": "message_contains", "text": "needle"}

    Supported actions:
      - {"type": "send_message", "channel": "<id|name>", "message_template": "..."}
      - {"type": "send_embed",  "channel": "<id|name>", "title": "...", "description": "...", "footer": "...", "color": "#5865F2"}

    Template context tokens (non-exhaustive):
      {now} {guild_name} {member_count}
      When a member is present:
      {user} {user_name} {user_discriminator} {user_mention} {joined_at}
    """

    def __init__(self, bot: discord.Client, flows: Dict[str, Any], guild_id: Optional[int] = None):
        self.bot = bot
        self.flows = flows.get("flows", [])
        self.guild_id = guild_id
        self._tasks: list[asyncio.Task] = []
        
    # --- FlowEngine additions ---
    def __init__(self, bot, flows, guild_id: int):
        self.bot = bot
        self.guild_id = int(guild_id)

        # in-memory state for Sprint D features
        self.reaction_role_map: dict[int, dict[str, int]] = {}
        self._ticket_cfgs: dict[int, dict] = {}
        self._schedule_tasks = getattr(self, "_schedule_tasks", [])

        self.set_flows(flows)

    def set_flows(self, flows_blob):
        """Accepts {'flows':[...]} or a direct list of flows."""
        if isinstance(flows_blob, dict):
            self.flows = list(flows_blob.get("flows") or [])
        else:
            self.flows = list(flows_blob or [])


    def _guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.guild_id)

    # ---------- TRIGGER HANDLERS ----------

    async def handle_message_regex(self, message: discord.Message):
        """Run flows whose trigger.type == 'message_regex' and pattern matches."""
        if not message.guild or message.author.bot:
            return
        guild = message.guild
        content = message.content or ""
        for f in self.flows or []:
            trg = (f.get("trigger") or {})
            if not self._is_enabled(f) or (trg.get("type") != "message_regex"):
                continue
            pattern = trg.get("pattern", "")
            flags = trg.get("flags", "")
            if not pattern:
                continue
            try:
                rx = re.compile(pattern, re.I if "i" in flags.lower() else 0)
            except re.error:
                continue
            m = rx.search(content)
            if not m:
                continue
            ctx = {
                "guild_id": str(guild.id),
                "channel_id": str(message.channel.id),
                "message_id": str(message.id),
                "content": content,
                "author_id": str(message.author.id),
                "author_mention": message.author.mention,
                "match_groups": list(m.groups()) if m.groups() else [],
            }
            await self._execute_flow(f, "message_regex", message.author, ctx)
            await self.handle_message_regex(message)

    async def handle_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # reaction-roles (panel-driven)
        mapping = self.reaction_role_map.get(payload.message_id)
        if mapping:
            emoji_key = payload.emoji.name if payload.emoji.is_unicode_emoji() else str(payload.emoji)
            role_id = mapping.get(emoji_key)
            if role_id:
                member = guild.get_member(payload.user_id)
                role = guild.get_role(role_id)
                if member and role and not member.bot:
                    try:
                        await member.add_roles(role, reason="reaction-roles opt-in")
                    except Exception as e:
                        print("[reaction_roles add] error:", e)

        # regex’d/filtered reaction-add flows
        for f in self.flows or []:
            trg = (f.get("trigger") or {})
            if not self._is_enabled(f) or (trg.get("type") != "reaction_add"):
                continue
            if trg.get("message_id") and int(trg["message_id"]) != payload.message_id:
                continue
            want = trg.get("emoji")
            if want:
                got = payload.emoji.name if payload.emoji.is_unicode_emoji() else str(payload.emoji)
                if str(want) != str(got):
                    continue
            channel_id = payload.channel_id
            ctx = {
                "guild_id": str(payload.guild_id),
                "channel_id": str(channel_id),
                "message_id": str(payload.message_id),
                "user_id": str(payload.user_id),
                "emoji": payload.emoji.name if payload.emoji.is_unicode_emoji() else str(payload.emoji),
            }
            user = guild.get_member(payload.user_id) or payload.user_id
            await self._execute_flow(f, "reaction_add", user, ctx)

    async def handle_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # reaction-roles removal
        mapping = self.reaction_role_map.get(payload.message_id)
        if mapping:
            emoji_key = payload.emoji.name if payload.emoji.is_unicode_emoji() else str(payload.emoji)
            role_id = mapping.get(emoji_key)
            if role_id:
                member = guild.get_member(payload.user_id)
                role = guild.get_role(role_id)
                if member and role and not member.bot:
                    try:
                        await member.remove_roles(role, reason="reaction-roles opt-out")
                    except Exception as e:
                        print("[reaction_roles remove] error:", e)

        for f in self.flows or []:
            trg = (f.get("trigger") or {})
            if not self._is_enabled(f) or (trg.get("type") != "reaction_remove"):
                continue
            if trg.get("message_id") and int(trg["message_id"]) != payload.message_id:
                continue
            want = trg.get("emoji")
            if want:
                got = payload.emoji.name if payload.emoji.is_unicode_emoji() else str(payload.emoji)
                if str(want) != str(got):
                    continue
            channel_id = payload.channel_id
            ctx = {
                "guild_id": str(payload.guild_id),
                "channel_id": str(channel_id),
                "message_id": str(payload.message_id),
                "user_id": str(payload.user_id),
                "emoji": payload.emoji.name if payload.emoji.is_unicode_emoji() else str(payload.emoji),
            }
            user = guild.get_member(payload.user_id) or payload.user_id
            await self._execute_flow(f, "reaction_remove", user, ctx)

    async def handle_member_update(self, before: discord.Member, after: discord.Member):
        """Fire role_added / role_removed triggers by diffing roles."""
        if before.guild.id != self.guild_id or before.bot:
            return
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        added = list(after_ids - before_ids)
        removed = list(before_ids - after_ids)

        for f in self.flows or []:
            trg = (f.get("trigger") or {})
            if not self._is_enabled(f):
                continue
            t = trg.get("type")
            if t == "role_added" and added:
                role_id = int(trg.get("role_id")) if trg.get("role_id") else None
                hit = added if role_id is None else [rid for rid in added if rid == role_id]
                for rid in hit:
                    ctx = {
                        "guild_id": str(self.guild_id),
                        "user_id": str(after.id),
                        "role_id": str(rid),
                        "role_name": (after.guild.get_role(rid).name if after.guild.get_role(rid) else ""),
                    }
                    await self._execute_flow(f, "role_added", after, ctx)
            if t == "role_removed" and removed:
                role_id = int(trg.get("role_id")) if trg.get("role_id") else None
                hit = removed if role_id is None else [rid for rid in removed if rid == role_id]
                for rid in hit:
                    ctx = {
                        "guild_id": str(self.guild_id),
                        "user_id": str(after.id),
                        "role_id": str(rid),
                        "role_name": (after.guild.get_role(rid).name if after.guild.get_role(rid) else ""),
                    }
                    await self._execute_flow(f, "role_removed", after, ctx)

    async def handle_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if getattr(channel, "guild", None) and channel.guild.id != self.guild_id:
            return
        for f in self.flows or []:
            trg = (f.get("trigger") or {})
            if self._is_enabled(f) and trg.get("type") == "channel_created":
                ctx = {
                    "guild_id": str(self.guild_id),
                    "channel_id": str(channel.id),
                    "channel_name": getattr(channel, "name", ""),
                }
                await self._execute_flow(f, "channel_created", getattr(channel, "creator", None), ctx)

    async def handle_thread_create(self, thread: discord.Thread):
        if thread.guild.id != self.guild_id:
            return
        for f in self.flows or []:
            trg = (f.get("trigger") or {})
            if self._is_enabled(f) and trg.get("type") == "thread_created":
                ctx = {
                    "guild_id": str(self.guild_id),
                    "channel_id": str(thread.id),
                    "channel_name": thread.name,
                }
                await self._execute_flow(f, "thread_created", getattr(thread, "owner", None), ctx)

    # ---------- EXTRA ACTIONS & LOGIC SUPPORT ----------

    async def _try_handle_extra_action(self, action: dict, ctx: dict) -> bool:
        """Return True if handled (so caller can `continue`)."""
        msg_type = (action.get("type") or action.get("kind") or "").lower()
        guild = self._guild()
        if not guild:
            return True  # no guild => skip

        # helper: resolve channel
        async def _resolve_channel():
            chan_ref = action.get("channel_id") or action.get("channel") or ctx.get("channel_id")
            return await find_channel(guild, chan_ref) if chan_ref else None

        # helper: resolve member target
        def _resolve_member():
            uid = action.get("user_id") or ctx.get("user_id") or ctx.get("author_id")
            return guild.get_member(int(uid)) if uid else None

        # ---- Messaging / Files ----
        if msg_type == "dm_user":
            m = _resolve_member()
            if not m:
                return False
            text = format_template(action.get("message_template") or "", ctx)
            try:
                await m.send(text[:2000])
            except Exception as e:
                print("[dm_user] error:", e)
            return True

        # ---- Simple public send_message to a channel ----
        if msg_type in {"send_message", "message", "send"}:
            ch = await _resolve_channel()
            if not ch:
                return False  # let caller handle (e.g., ephemeral)
            text = format_template(action.get("message") or action.get("message_template") or "", ctx)
            await ch.send(text[:2000] if text else "\u200b")
            return True

        # ---- Simple public send_embed to a channel ----
        if msg_type == "send_embed":
            ch = await _resolve_channel()
            if not ch:
                return False
            title = format_template(action.get("title") or "", ctx)
            desc  = format_template(action.get("description") or action.get("embed_description") or "", ctx)
            col   = action.get("color") or action.get("embed_color") or "#5865F2"
            try:
                col = int(str(col).replace("#", ""), 16)
            except Exception:
                col = 0x5865F2
            embed = discord.Embed(title=title, description=desc, color=col)
            if action.get("footer"):
                embed.set_footer(text=format_template(action["footer"], ctx))
            await ch.send(embed=embed)
            return True



        if msg_type in {"upload_file", "image_upload", "file_upload"}:
            ch = await _resolve_channel()
            if not ch:
                return False
            url = format_template(action.get("url") or "", ctx)
            caption = format_template(action.get("content") or "", ctx)
            filename = action.get("filename") or (url.rsplit("/", 1)[-1] if url else "file.bin")
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(url) as r:
                        data = await r.read()
                file = discord.File(io.BytesIO(data), filename=filename)
                await ch.send(content=caption[:2000] if caption else None, file=file)
            except Exception as e:
                print("[upload_file] error:", e)
            return True




        # ---- Roles / Moderation ----
        if msg_type in {"role_add", "role_remove", "add_role", "remove_role"}:
            m = _resolve_member()
            role_ref = action.get("role_id") or action.get("role") or action.get("role_name")
            role = guild.get_role(int(role_ref)) if str(role_ref).isdigit() else discord.utils.get(guild.roles, name=str(role_ref))
            if not (m and role):
                return True
            try:
                if "remove" in msg_type:
                    await m.remove_roles(role, reason=action.get("reason") or "flow role remove")
                else:
                    await m.add_roles(role, reason=action.get("reason") or "flow role add")
            except Exception as e:
                print("[role add/remove] error:", e)
            return True

        if msg_type == "timeout":
            m = _resolve_member()
            seconds = int(action.get("seconds") or 0)
            reason = action.get("reason") or "flow timeout"
            if m and seconds > 0:
                until = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
                try:
                    await m.timeout(until, reason=reason)
                except Exception as e:
                    print("[timeout] error:", e)
            return True

        if msg_type == "kick":
            m = _resolve_member()
            reason = action.get("reason") or "flow kick"
            if m:
                try:
                    await m.kick(reason=reason)
                except Exception as e:
                    print("[kick] error:", e)
            return True

        if msg_type == "ban":
            m = _resolve_member()
            reason = action.get("reason") or "flow ban"
            delete_message_days = int(action.get("delete_message_days") or 0)
            if m:
                try:
                    await guild.ban(m, delete_message_days=delete_message_days, reason=reason)
                except Exception as e:
                    print("[ban] error:", e)
            return True

        # ---- Reaction roles panel ----
        if msg_type in {"reaction_roles", "reactionroles"}:
            ch = await _resolve_channel()
            if not ch:
                return True
            content = format_template(action.get("content") or action.get("message") or "React to get roles", ctx)
            mapping: list[dict] = action.get("map") or action.get("mapping") or []
            msg = await ch.send(content[:2000])
            emoji_to_role: dict[str, int] = {}
            for item in mapping:
                emoji = str(item.get("emoji"))
                role_id = int(item.get("role_id")) if item.get("role_id") else None
                if not (emoji and role_id):
                    continue
                try:
                    await msg.add_reaction(emoji)
                    emoji_to_role[emoji] = role_id
                except Exception as e:
                    print("[reaction_roles add_reaction] error:", e)
            if emoji_to_role:
                self.reaction_role_map[msg.id] = emoji_to_role
            return True

        # ---- Pagination ----
        if msg_type == "paginate":
            ch = await _resolve_channel()
            if not ch:
                return False
            pages = action.get("pages") or ctx.get(action.get("items_var") or "pages") or []
            pages = [str(format_template(p, ctx)) for p in pages] if isinstance(pages, list) else [str(pages)]
            if not pages:
                return True
            use_embed = bool(action.get("embed"))
            if use_embed:
                embed = discord.Embed(description=pages[0], color=discord.Color.blurple())
                await ch.send(embed=embed, view=self._make_paginator_view(embed, pages))
            else:
                await ch.send(content=pages[0][:2000], view=self._make_paginator_view(None, pages))
            return True

        # ---- Tickets ----
        if msg_type in {"ticket_panel", "ticket_post"}:
            ch = await _resolve_channel()
            if not ch:
                return True
            content = format_template(action.get("content") or "Open a ticket with the button below.", ctx)
            cfg = {
                "staff_role_id": int(action.get("staff_role_id")) if action.get("staff_role_id") else None,
                "category_id": int(action.get("category_id")) if action.get("category_id") else None,
                "transcript_channel_id": int(action.get("transcript_channel_id")) if action.get("transcript_channel_id") else None,
            }
            view = self._make_ticket_open_view(cfg)
            msg = await ch.send(content[:2000], view=view)
            self._ticket_cfgs[msg.id] = cfg
            return True

        # unknown => not handled
        return False

    def _make_paginator_view(self, embed: discord.Embed | None, pages: list[str]) -> discord.ui.View:
        class Pager(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.i = 0

            @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
            async def prev(self, inter: discord.Interaction, btn: discord.ui.Button):
                if self.i > 0:
                    self.i -= 1
                    if embed:
                        embed.description = pages[self.i]
                        await inter.response.edit_message(embed=embed, view=self)
                    else:
                        await inter.response.edit_message(content=pages[self.i], view=self)

            @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
            async def nxt(self, inter: discord.Interaction, btn: discord.ui.Button):
                if self.i < len(pages) - 1:
                    self.i += 1
                    if embed:
                        embed.description = pages[self.i]
                        await inter.response.edit_message(embed=embed, view=self)
                    else:
                        await inter.response.edit_message(content=pages[self.i], view=self)
        return Pager()

    def _make_ticket_open_view(self, cfg: dict) -> discord.ui.View:
        engine = self
        class OpenTicket(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary)
            async def open(self, inter: discord.Interaction, btn: discord.ui.Button):
                guild = engine._guild()
                if not guild:
                    return await inter.response.send_message("Guild missing.", ephemeral=True)
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                }
                if cfg.get("staff_role_id"):
                    staff = guild.get_role(cfg["staff_role_id"])
                    if staff:
                        overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                category = guild.get_channel(cfg["category_id"]) if cfg.get("category_id") else None
                name = f"ticket-{inter.user.name[:16]}-{str(inter.user.id)[-4:]}"
                ch = await guild.create_text_channel(name=name, category=category, overwrites=overwrites, reason="ticket created")
                await inter.response.send_message(f"Created {ch.mention}", ephemeral=True)
                await ch.send(f"{inter.user.mention} thanks for opening a ticket. A staff member will be with you shortly.", view=engine._make_ticket_close_view(cfg, ch.id))
        return OpenTicket()

    def _make_ticket_close_view(self, cfg: dict, channel_id: int) -> discord.ui.View:
        engine = self
        class CloseTicket(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger)
            async def close(self, inter: discord.Interaction, btn: discord.ui.Button):
                guild = engine._guild()
                ch = guild.get_channel(channel_id) if guild else None
                if not ch:
                    return await inter.response.send_message("Channel not found.", ephemeral=True)
                # make simple transcript
                lines = []
                async for m in ch.history(limit=200, oldest_first=True):
                    ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    content = (m.content or "").replace("\n", " ")
                    lines.append(f"[{ts}] {m.author.display_name}: {content}")
                transcript = "\n".join(lines) or "No messages."
                # send transcript
                if cfg.get("transcript_channel_id"):
                    dest = guild.get_channel(cfg["transcript_channel_id"])
                    if dest:
                        fp = io.BytesIO(transcript.encode("utf-8"))
                        await dest.send(file=discord.File(fp, filename=f"transcript-{channel_id}.txt"))
                try:
                    await inter.response.send_message("Closing ticket…", ephemeral=True)
                except Exception:
                    pass
                await ch.delete(reason="ticket closed")
        return CloseTicket()


        # flows can be disabled at runtime (in-memory)
        self._disabled_ids: set[str] = {
            str(f.get("id")) for f in self.flows if f.get("enabled") is False
        }

    def _is_enabled(self, flow: dict) -> bool:
        fid = str(flow.get("id", ""))
        if fid and fid in self._disabled_ids:
            return False
        # also respect an explicit "enabled": false in the flow payload
        return flow.get("enabled", True) is not False

    def set_flow_enabled(self, flow_id: str, enabled: bool) -> bool:
        """Enable/disable by id; returns True if a matching flow existed."""
        found = False
        for f in self.flows:
            if str(f.get("id")) == str(flow_id):
                found = True
                if enabled:
                    self._disabled_ids.discard(str(flow_id))
                    f["enabled"] = True
                else:
                    self._disabled_ids.add(str(flow_id))
                    f["enabled"] = False
        return found

    def toggle_flow(self, flow_id: str) -> bool:
        for f in self.flows:
            if str(f.get("id")) == str(flow_id):
                now = not self._is_enabled(f)
                self.set_flow_enabled(flow_id, now)
                return now
        return False

    def _guilds(self):
        if self.guild_id:
            g = self.bot.get_guild(self.guild_id)
            return [g] if g else []
        return list(self.bot.guilds)

    # ---------- Scheduling lifecycle ----------

    async def start_schedules(self):
        """Start background tasks for all schedule triggers."""
        for flow in self.flows:
            if not self._is_enabled(flow):
                continue
            trig = flow.get("trigger", {})
            if trig.get("type") == "schedule":
                interval = int(trig.get("interval_seconds", 3600))
                self._tasks.append(asyncio.create_task(self._schedule_runner(flow, interval)))


    async def cancel_schedules(self):
        """Cancel all running schedule tasks."""
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            # Let tasks observe the cancellation without raising
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def restart_schedules(self):
        """Cancel then start schedules based on current self.flows."""
        await self.cancel_schedules()
        await self.start_schedules()

    def set_flows(self, flows: Dict[str, Any]):
        """Replace in-memory flows. Call restart_schedules() afterwards to apply."""
        self.flows = flows.get("flows", [])

    async def _schedule_runner(self, flow: Dict[str, Any], interval: int):
        await self.bot.wait_until_ready()
        try:
            while not self.bot.is_closed():
                try:
                    if self._is_enabled(flow):
                        await self._execute_flow(flow, "schedule")
                except Exception as e:
                    print("[FlowEngine] schedule error:", e)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


    # ---------- Event handlers ----------

    async def handle_member_join(self, member: discord.Member):
        for flow in self.flows:
            if not self._is_enabled(flow):
                continue
            if flow.get("trigger", {}).get("type") == "member_join":
                try:
                    await self._execute_flow(flow, "member_join", member)
                except Exception as e:
                    print("[FlowEngine] member_join error:", e)

    async def handle_message(self, message: Message):
        """Dispatch flows that trigger on message content."""
        if message.author.bot:
            return
        content = (message.content or "")
        if not content:
            return

        for flow in self.flows:
            if not self._is_enabled(flow):
                continue
            trig = flow.get("trigger", {})
            if trig.get("type") != "message_contains":
                continue

            needle = (trig.get("text") or "").strip()
            if not needle:
                continue

            try:
                if needle.lower() in content.lower():
                    # Best-effort pass of 'member' so templates can render user tokens.
                    member = message.author if isinstance(message.author, discord.Member) else None
                    await self._execute_flow(flow, "message_contains", member)
            except Exception as e:
                print("[FlowEngine] message_contains error:", e)

    async def test_flows(self, channel: discord.TextChannel):
        await channel.send("🔧 Running test flows...")
        for flow in self.flows:
            if not self._is_enabled(flow):
                continue
            
            try:
                await self._execute_flow(flow, "manual_test")
            except Exception as e:
                await channel.send(f"[FlowEngine] Test error: {e}")

    # ---------- Execution ----------

    async def _execute_flow(
        self,
        flow: Dict[str, Any],
        context_source: str,
        member: Optional[discord.Member] = None
    ):
        actions = flow.get("actions") or flow.get("steps") or []
        if not actions and "action" in flow:  # legacy single action
            actions = [flow["action"]]

        for action in actions:
            msg_type = action.get("type")
            if msg_type not in {"send_message", "send_embed", "fetch_api", "steps"}:
                continue  # skip unsupported
            handled = await self._try_handle_extra_action(action, ctx)
            if handled:
                continue


            for guild in self._guilds():
                if not guild:
                    continue

                # build context
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ctx = {
                    "now": now,
                    "guild_name": guild.name,
                    "member_count": getattr(guild, "member_count", None) or "unknown",
                }
                if member and isinstance(member, discord.Member) and member.guild and guild and member.guild.id == guild.id:
                    ctx.update({
                        "user": f"{member.name}#{member.discriminator}",
                        "user_name": member.name,
                        "user_discriminator": member.discriminator,
                        "user_mention": member.mention,
                        "joined_at": getattr(member, "joined_at", None),
                    })

                # resolve channel
                channel_ref = action.get("channel_id") or action.get("channel") or "#general"
                channel = await find_channel(guild, channel_ref)

                if not channel:
                    print(f"[FlowEngine] Channel not found in '{guild.name}': {channel_ref}")
                    continue

                # ---- execute each action type ----
                if msg_type == "send_message":
                    template = action.get("message_template", "Hello!")
                    await channel.send(format_template(template, ctx))

                elif msg_type == "send_embed":
                    title = format_template(action.get("title", "Notification"), ctx)
                    desc  = format_template(action.get("description", ""), ctx)
                    color = discord.Color.blurple()
                    try:
                        if "color" in action:
                            cval = action["color"]
                            color = cval if isinstance(cval, int) else int(str(cval).replace("#", ""), 16)
                            color = discord.Color(color)
                    except Exception:
                        pass
                    embed = discord.Embed(title=title, description=desc, color=color)
                    footer = action.get("footer")
                    if footer:
                        embed.set_footer(text=format_template(footer, ctx))
                    await channel.send(embed=embed)
                continue
            if msg_type == "fetch_api":
                # Build context (already set above)
                method = (action.get("method") or "GET").upper()
                url    = format_template(action.get("url") or "", ctx)
                headers = {k: format_template(str(v), ctx) for k,v in _parse_json_obj(action.get("headers")).items()}
                params  = _parse_json_obj(action.get("params"))
                body    = _parse_json_obj(action.get("body"))
                timeout_ms  = int(action.get("timeout_ms") or 8000)
                expect_json = bool(action.get("expect_json") if action.get("expect_json") is not None else True)
                json_path   = action.get("json_path") or ""
                reply_mode  = (action.get("reply_mode") or "message").lower()
                guild_id_for_scope = str(guild.id) if guild else None
                url     = _inject_secrets_in_obj(url,     guild_id_for_scope)
                headers = _inject_secrets_in_obj(headers, guild_id_for_scope)
                params  = _inject_secrets_in_obj(params,  guild_id_for_scope)
                body    = _inject_secrets_in_obj(body,    guild_id_for_scope)

                
                timeout = aiohttp.ClientTimeout(total=timeout_ms/1000)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    req = dict(url=url, headers=headers or None)
                    if method == "GET":
                        req["params"] = params or None
                    else:
                        payload = body if body else (params or None)
                        if payload is not None:
                            req["json"] = payload
                    async with s.request(method, **req) as r:
                        text = await r.text()
                        data = None
                        if expect_json:
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = {"_raw": text}

                extracted = data
                if expect_json and json_path:
                    extracted = _dot_get(data, json_path)

                if expect_json:
                    from json import dumps
                    if isinstance(extracted, (dict, list)):
                        rendered_data = dumps(extracted, ensure_ascii=False)[:1800]
                    else:
                        rendered_data = str(extracted)
                else:
                    rendered_data = (extracted if isinstance(extracted, str) else text)[:1800]

                chunks = _smart_format(extracted, action, {**ctx})
                if reply_mode == "embed":
                    title = format_template(action.get("embed_title") or "API Result", ctx)
                    embed = discord.Embed(title=title, description=chunks[0], color=discord.Color.blurple())
                    await channel.send(embed=embed)
                    for ch in chunks[1:]:
                        await channel.send(ch[:2000])
                else:
                    await channel.send(chunks[0][:2000])
                    for ch in chunks[1:]:
                        await channel.send(ch[:2000])
                continue
            elif msg_type == "steps":
                # Build a starting context (same keys you use elsewhere)
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ctx.update({
                    "now": now,
                    "guild_name": guild.name,
                    "member_count": getattr(guild, "member_count", None) or "unknown",
                })


                # BEFORE running run_steps(...)
                # (Place this just before `await run_steps(...)` in the "steps" branch)
                chan = action.get("channel_id") or action.get("channel")
                if chan:
                    ctx["channel_id"] = str(chan)

                # Run the toolbox steps; _send_step will use self.bot to send to channels
                await run_steps(Ctx(ctx), action.get("steps", []), discord_client=self.bot)
                continue


