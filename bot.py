from dotenv import load_dotenv
load_dotenv()
import os
import aiohttp
import discord
from discord import app_commands
from aiohttp import web
from pathlib import Path
from autosync import AutoSyncManager
import asyncio
import json, re
from typing import Any, List, Dict
from config import get_settings
from engine import FlowEngine, format_template, run_steps, Ctx
from discord.ui import View, button
from discord import ButtonStyle


# bot.py — add near the top
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")

async def fetch_guild_flows(gid: int) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{API_URL}/guilds/{gid}/flows",
            timeout=10,
            headers={"X-DASHBOARD-SECRET": DASHBOARD_SECRET} if DASHBOARD_SECRET else None,
        ) as r:
            if r.status != 200:
                return {"flows": []}
            return await r.json()

async def fetch_guild_commands(gid: int) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{API_URL}/guilds/{gid}/commands",
            timeout=10,
            headers={"X-DASHBOARD-SECRET": DASHBOARD_SECRET} if DASHBOARD_SECRET else None,
        ) as r:
            if r.status != 200:
                return {"commands": []}
            return await r.json()



WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5001"))


API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")
DASHBOARD_API_URL = os.getenv("DASHBOARD_API_URL", "http://127.0.0.1:8000")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.reactions = True

def make_paginator_view(embed, pages):
    class Pager(View):
        def __init__(self):
            super().__init__(timeout=120)
            self.i = 0

        @button(label="Prev", style=ButtonStyle.secondary)
        async def prev(self, inter, btn):
            if self.i > 0:
                self.i -= 1
                if embed:
                    embed.description = pages[self.i]
                    await inter.response.edit_message(embed=embed, view=self)
                else:
                    await inter.response.edit_message(content=pages[self.i], view=self)

        @button(label="Next", style=ButtonStyle.primary)
        async def nxt(self, inter, btn):
            if self.i < len(pages) - 1:
                self.i += 1
                if embed:
                    embed.description = pages[self.i]
                    await inter.response.edit_message(embed=embed, view=self)
                else:
                    await inter.response.edit_message(content=pages[self.i], view=self)
    return Pager()

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
    """Very small dot/index path reader, e.g. data.items[0].name"""
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

def _first_tabular_path(data, path="$"):
    """
    Walk the JSON and return (path, value) where value is a list of dicts
    or a list of simple values. Falls back to the first dict if found.
    """
    from collections.abc import Mapping, Sequence
    # 1) exact match first
    if isinstance(data, list) and (not data or isinstance(data[0], (Mapping, list, str, int, float, bool, type(None)))):
        return path, data
    # 2) search inside dicts
    if isinstance(data, Mapping):
        for k, v in data.items():
            p, val = _first_tabular_path(v, f"{path}.{k}" if path != "$" else k)
            if val is not None:
                return p, val
    # 3) search inside lists
    if isinstance(data, list):
        for i, v in enumerate(data[:5]):  # sample a few
            p, val = _first_tabular_path(v, f"{path}[{i}]")
            if val is not None:
                return p, val
    return None, None


class MyClient(discord.Client):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.tree = app_commands.CommandTree(self)
        self.autosync = None  # will be set in setup_hook
        # one FlowEngine per guild id
        self.engines: dict[int, FlowEngine] = {}
        # track API-driven cmd names per guild (for deletion)
        self._api_cmd_names: dict[int, set[str]] = {}
        # bootstrap guards
        self._bootstrapped: bool = False
        self._guild_bootstrapped: set[int] = set()

    # ---------- Lifecycle ----------
    # ... inside class MyClient

    # TRIGGERS: Regex message match
    async def on_message(self, message: discord.Message):
        # keep your existing on_message content if present, then:
        eng = self.engines.get(message.guild.id) if message.guild else None
        if eng:
            await eng.handle_message_regex(message)

    # TRIGGERS: Reaction add/remove (raw so it works without cache)
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        eng = self.engines.get(payload.guild_id) if payload.guild_id else None
        if eng:
            await eng.handle_reaction_add(payload)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        eng = self.engines.get(payload.guild_id) if payload.guild_id else None
        if eng:
            await eng.handle_reaction_remove(payload)

    # TRIGGERS: Role changes
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        eng = self.engines.get(after.guild.id)
        if eng:
            await eng.handle_member_update(before, after)

    # TRIGGERS: Channel/thread created
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        eng = self.engines.get(channel.guild.id)
        if eng:
            await eng.handle_guild_channel_create(channel)

    async def on_thread_create(self, thread: discord.Thread):
        eng = self.engines.get(thread.guild.id)
        if eng:
            await eng.handle_thread_create(thread)


    async def setup_hook(self):
        # one-time global cleanup of legacy globals
        await self._clean_old_global_commands({"runflow", "testflow", "reloadflows", "reloadcommands"})

        # start the webhook once
        if not getattr(self, "_web_started", False):
            await start_webhook_server(self)
            self._web_started = True

        # DEFER the bootstrap until the client is fully ready
        if not getattr(self, "_boot_task_started", False):
            self._boot_task_started = True
            asyncio.create_task(self._post_ready_bootstrap())

    async def _post_ready_bootstrap(self):
        # wait until Discord has populated self.guilds
        await self.wait_until_ready()
        # safety: if we reconnected, avoid re-running
        if not getattr(self, "_bootstrapped", False):
            print(f"[bootstrap] starting; guilds seen: {len(self.guilds)}")
            await self._bootstrap_all_guilds()
            self._bootstrapped = True
            print("[bootstrap] commands & flows registered for all guilds")




    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("------")
        # Double-ensure after cache is definitely ready
        if not getattr(self, "_bootstrapped", False):
            await self._bootstrap_all_guilds()
            self._bootstrapped = True
            print("[bootstrap] late-run after on_ready")    


    async def on_guild_join(self, guild: discord.Guild):
        await self._ensure_guild_setup(guild.id)

    async def on_guild_remove(self, guild: discord.Guild):
        # clean up engine and command tracking
        eng = self.engines.pop(guild.id, None)
        if eng:
            await eng.cancel_schedules()
        self._api_cmd_names.pop(guild.id, None)
        self._guild_bootstrapped.discard(guild.id)

    async def _bootstrap_all_guilds(self):
        for g in list(self.guilds):
            await self._ensure_guild_setup(g.id)
        self._bootstrapped = True

    # ---------- Per-guild bootstrap ----------
    async def _ensure_guild_setup(self, gid: int):
        # prevent duplicate concurrent setup
        if gid in self._guild_bootstrapped:
            return
        self._guild_bootstrapped.add(gid)
        try:
            # FlowEngine for this guild
            flows = await fetch_guild_flows(gid)
            eng = FlowEngine(bot=self, flows=flows, guild_id=gid)
            self.engines[gid] = eng

            guild_obj = discord.Object(id=gid)

            # idempotent: ensure no leftover /pickflow for this guild
            self.tree.remove_command("pickflow", type=discord.AppCommandType.chat_input, guild=guild_obj)

            # /pickflow — choose a flow, run it, or toggle enabled state
            @self.tree.command(name="pickflow", description="Pick a flow to run or toggle", guild=guild_obj)
            async def pickflow(inter: discord.Interaction):
                if inter.guild is None:
                    await inter.response.send_message("Run this in a server.", ephemeral=True)
                    return

                e = self.engines.get(inter.guild.id)
                if not e or not e.flows:
                    await inter.response.send_message("No flows are configured for this server.", ephemeral=True)
                    return

                # Build Select options from flows
                options = []
                for f in e.flows:
                    fid = str(f.get("id") or "")
                    name = (f.get("name") or f.get("title") or fid or "unnamed")[:100]
                    enabled = e._is_enabled(f)
                    label = f"{name} [{'ON' if enabled else 'OFF'}]"
                    desc = (f.get("description") or f.get("action", {}).get("message_template") or "")[:100]
                    options.append(discord.SelectOption(label=label, description=desc or None, value=fid or name))

                class PickView(discord.ui.View):
                    def __init__(self, eng: FlowEngine, *, timeout: float | None = 60):
                        super().__init__(timeout=timeout)
                        self.eng = eng
                        self.selected_id: str | None = None

                    @discord.ui.select(placeholder="Select a flow…", min_values=1, max_values=1, options=options)
                    async def select_flow(self, inter2: discord.Interaction, select: discord.ui.Select):
                        self.selected_id = select.values[0]
                        enabled = False
                        for f in self.eng.flows:
                            if str(f.get("id") or "") == self.selected_id or (not f.get("id") and f.get("name") == self.selected_id):
                                enabled = self.eng._is_enabled(f)
                                break
                        for child in self.children:
                            if isinstance(child, discord.ui.Button) and child.custom_id == "toggle":
                                child.label = "Turn OFF" if enabled else "Turn ON"
                        await inter2.response.edit_message(content=f"Selected flow: `{self.selected_id}`", view=self)

                    @discord.ui.button(label="Run Now", style=discord.ButtonStyle.primary, custom_id="run")
                    async def run_now(self, inter2: discord.Interaction, btn: discord.ui.Button):
                        if not self.selected_id:
                            await inter2.response.send_message("Pick a flow first.", ephemeral=True)
                            return
                        flow = next((f for f in self.eng.flows if str(f.get("id") or "") == self.selected_id or (not f.get("id") and f.get("name") == self.selected_id)), None)
                        if not flow:
                            await inter2.response.send_message("Flow not found (it may have been removed).", ephemeral=True)
                            return
                        try:
                            await self.eng._execute_flow(flow, "manual_run", inter2.user if isinstance(inter2.user, discord.Member) else None)
                            await inter2.response.send_message("✅ Flow executed.", ephemeral=True)
                        except Exception as ex:
                            await inter2.response.send_message(f"❌ Error: {ex}", ephemeral=True)

                    @discord.ui.button(label="Turn ON/OFF", style=discord.ButtonStyle.secondary, custom_id="toggle")
                    async def toggle(self, inter2: discord.Interaction, btn: discord.ui.Button):
                        if not self.selected_id:
                            await inter2.response.send_message("Pick a flow first.", ephemeral=True)
                            return
                        new_state = self.eng.toggle_flow(self.selected_id)
                        await self.eng.restart_schedules()  # apply for schedule triggers
                        try:
                            # ✅ REPLACE the body of the PATCH call inside PickView.toggle(...)
                            import aiohttp
                            api = os.getenv("DASHBOARD_API_URL", "http://127.0.0.1:8000")
                            url = f"{api}/guilds/{inter2.guild.id}/flows/enabled"
                            payload = {"updates": [{"id": self.selected_id, "enabled": new_state}]}
                            headers = {"X-DASHBOARD-SECRET": os.getenv("DASHBOARD_SECRET", "")}
                            async with aiohttp.ClientSession() as s:
                                await s.patch(url, json=payload, timeout=5, headers=headers)

                        except Exception as e:
                            print("[pickflow] persist toggle failed:", e)
                        btn.label = "Turn OFF" if new_state else "Turn ON"
                        await inter2.response.edit_message(content=f"Toggled `{self.selected_id}` → {'ON' if new_state else 'OFF'}", view=self)

                view = PickView(e)
                await inter.response.send_message("Pick a flow, then choose an action:", view=view, ephemeral=True)

            # API-driven commands
            cmds = await fetch_guild_commands(gid)
            await self._register_api_commands(gid, cmds.get("commands", []))

            # start schedules, then guild sync (instant)
            await eng.start_schedules()
            await self.tree.sync(guild=guild_obj)

        except Exception:
            # allow retry if something failed midway
            self._guild_bootstrapped.discard(gid)
            raise


# ---------- API-driven commands per guild ----------
    async def _register_api_commands(self, gid: int, commands: list[dict]):
        guild = discord.Object(id=gid)

        async def _resolve_cmd_channel(inter: discord.Interaction, ch_ref: str | int | None):
            """
            For commands:
            - No channel / blank  -> use the interaction's channel
            - 'here'/'current'    -> interaction channel
            - Otherwise           -> resolve by id or name inside the guild
            """
            # treat None / "" as "here"
            token = (str(ch_ref).strip().lower() if ch_ref is not None else "")
            if (not token) or token in {"here", "current", "this", "same"}:
                return inter.channel if isinstance(inter.channel, discord.TextChannel) else None

            if inter.guild:
                from engine import find_channel
                return await find_channel(inter.guild, str(ch_ref))
            return None



        def make_callback(cmddef: dict):
            async def callback(inter: discord.Interaction):
                try:
                    await inter.response.defer(ephemeral=True)
                except Exception:
                    pass

                responded = False
                posted_to_channel = False

                async def send_ephemeral_once(*, content: str | None = None,
                                            embed: discord.Embed | None = None,
                                            file: discord.File | None = None):
                    nonlocal responded
                    kwargs = {}
                    if embed is not None:
                        kwargs["embed"] = embed
                    if file is not None:
                        kwargs["file"] = file
                    if not responded:
                        kwargs["ephemeral"] = True
                        responded = True
                    await inter.followup.send(content=content, **kwargs)

                guild_obj = inter.guild
                now = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                ctx = {
                    "now": now,
                    "guild_id": str(guild_obj.id) if guild_obj else None,
                    "channel_id": str(inter.channel.id) if inter.channel else None,
                    "user_id": str(inter.user.id),
                    "user_mention": inter.user.mention,
                    "guild_name": guild_obj.name if guild_obj else "DM",
                    "member_count": getattr(guild_obj, "member_count", None) or "unknown",
                }

                eng = self.engines.get(guild_obj.id) if guild_obj else None
                if not eng:
                    return await send_ephemeral_once(content="⚠️ Engine not ready.")

                # ---- resolve actions (supports {spec:{actions:[...]}} ) ----
                root = cmddef.get("spec") or cmddef.get("action") or cmddef
                if isinstance(root, dict) and isinstance(root.get("actions"), list):
                    actions_list = root["actions"]
                elif isinstance(root, list):
                    actions_list = root
                elif isinstance(root, dict) and ("type" in root or "kind" in root):
                    actions_list = [root]
                else:
                    actions_list = []

                try:
                    for action in (actions_list or []):
                        # Let engine try its own advanced handlers first
                        try:
                            handled = await eng._try_handle_extra_action(action, ctx)
                        except Exception:
                            handled = False
                        if handled:
                            posted_to_channel = True
                            continue

                        at = (action.get("type") or action.get("kind") or "").lower()

                        # --- SEND EMBED ---
                        if at == "send_embed":
                            title = format_template(action.get("title") or "Title", ctx)
                            desc  = format_template(action.get("description") or action.get("embed_description") or "Description", ctx)
                            color = action.get("color") or action.get("embed_color") or "#5865F2"
                            try:
                                col = int(str(color).replace("#", ""), 16)
                            except Exception:
                                col = 0x5865F2
                            embed = discord.Embed(title=title, description=desc, color=col)
                            if action.get("footer"):
                                embed.set_footer(text=format_template(action["footer"], ctx))

                            ch_ref = action.get("channel_id") or action.get("channel")
                            ch = await _resolve_cmd_channel(inter, ch_ref)
                            if ch:
                                await ch.send(embed=embed)
                                posted_to_channel = True
                            else:
                                # fallback to ephemeral if we couldn’t post to a channel (e.g., DMs)
                                await send_ephemeral_once(embed=embed)

                        # --- SEND MESSAGE ---
                        elif at == "send_message":
                            msg = format_template(action.get("message_template") or action.get("message") or "Hello!", ctx)
                            ch = await _resolve_cmd_channel(inter, action.get("channel_id") or action.get("channel"))
                            if ch:
                                await ch.send(msg[:2000])
                                posted_to_channel = True
                            else:
                                await send_ephemeral_once(content=msg[:2000])

                        # --- FETCH API ---
                        elif at == "fetch_api":
                            import aiohttp, json, re
                            method = (action.get("method") or "GET").upper()
                            url    = format_template(action.get("url") or "", ctx).strip()
                            if not (url.startswith("http://") or url.startswith("https://")):
                                await send_ephemeral_once(content="❌ URL must start with http:// or https://")
                                continue

                            def _maybe_json(x, default):
                                if isinstance(x, (dict, list)): return x
                                if isinstance(x, str) and x.strip():
                                    try: return json.loads(x)
                                    except Exception: return default
                                return default

                            headers = _maybe_json(action.get("headers"), {})
                            params  = _maybe_json(action.get("params"), {})
                            data    = _maybe_json(action.get("body"), None)
                            timeout_ms = int(action.get("timeout_ms") or 6000)
                            expect_json = bool(action.get("expect_json") if "expect_json" in action else True)
                            json_path   = action.get("json_path") or ""
                            reply_mode  = (action.get("reply_mode") or "message").lower()

                            try:
                                async with aiohttp.ClientSession() as s:
                                    to = aiohttp.ClientTimeout(total=timeout_ms/1000)
                                    async with s.request(method, url, headers=headers,
                                                        params=(None if data else params), json=data, timeout=to) as r:
                                        payload = (await r.json(content_type=None)) if expect_json else (await r.text())
                            except Exception as e:
                                await send_ephemeral_once(content=f"❌ Request error: {e}")
                                continue

                            def _extract(obj, path):
                                if not path: return obj
                                try:
                                    cur = obj
                                    for part in re.findall(r"[A-Za-z0-9_]+|\[[0-9]+\]", path):
                                        if part.startswith("["): cur = cur[int(part[1:-1])]
                                        else: cur = cur.get(part)
                                    return cur
                                except Exception:
                                    return obj

                            data_sel = _extract(payload, json_path) if json_path else payload

                            def _stringify(x):
                                if isinstance(x, (dict, list)): return json.dumps(x, ensure_ascii=False)[:1800]
                                return str(x)

                            if reply_mode == "embed":
                                t = format_template(action.get("embed_title") or "API Result", ctx)
                                d = format_template(action.get("embed_description") or "{data}", {**ctx, "data": _stringify(data_sel)})
                                color = action.get("embed_color") or "#5865F2"
                                try: col = int(color, 16) if (isinstance(color, str) and color.startswith("#")) else int(color)
                                except Exception: col = 0x5865F2
                                await send_ephemeral_once(embed=discord.Embed(title=t, description=d, color=col))
                            else:
                                msg_tmpl = action.get("message_template") or "API says: {data}"
                                msg = format_template(msg_tmpl, {**ctx, "data": _stringify(data_sel)})
                                await send_ephemeral_once(content=msg[:2000])

                        # --- DM USER ---
                        elif at == "dm_user":
                            user_id = action.get("user_id")
                            target  = inter.user if not user_id else (inter.guild.get_member(int(user_id)) if inter.guild else None)
                            if not target:
                                await send_ephemeral_once(content="⚠️ Couldn’t resolve user."); continue
                            content = format_template(action.get("message") or action.get("message_template") or "Hello!", ctx)
                            try:
                                await target.send(content[:2000])
                                await send_ephemeral_once(content="📨 Sent DM.")
                            except discord.Forbidden:
                                await send_ephemeral_once(content="❌ User has DMs closed.")

                        # --- UPLOAD FILE / IMAGE ---
                        elif at in ("upload_file", "upload_image", "send_image"):
                            import aiohttp, io
                            url = format_template(action.get("url") or action.get("image_url") or "", ctx).strip()
                            filename = action.get("filename") or "file"
                            if not (url.startswith("http://") or url.startswith("https://")):
                                await send_ephemeral_once(content="❌ File URL must start with http(s)://"); continue
                            async with aiohttp.ClientSession() as s:
                                async with s.get(url, timeout=20) as r:
                                    r.raise_for_status()
                                    data = await r.read()
                            file = discord.File(io.BytesIO(data), filename=filename)
                            caption = format_template(action.get("caption") or action.get("message_template") or "", ctx) or None

                            ch = await _resolve_cmd_channel(inter, action.get("channel_id") or action.get("channel"))
                            if ch:
                                await ch.send(content=caption, file=file)
                                posted_to_channel = True
                            else:
                                await send_ephemeral_once(content=caption, file=file)
                        # --- PAGINATE ---
                        elif at in ("paginate", "paginated_response"):
                            pages = action.get("pages")
                            items = action.get("items")
                            if not pages and items:
                                text = "\n".join(f"• {format_template(str(x), ctx)}" for x in items)
                                pages = _chunk_text(text, 900)
                            pages = pages or ["(no content)"]
                            mode = (action.get("mode") or "embed").lower()
                            if mode == "embed":
                                embed = discord.Embed(
                                    title=format_template(action.get("title") or "Pages", ctx),
                                    description=pages[0],
                                    color=0x5865F2,
                                )
                                await send_ephemeral_once(embed=embed)
                                await inter.followup.send(view=make_paginator_view(embed, pages))
                            else:
                                await send_ephemeral_once(content=pages[0])
                                await inter.followup.send(view=make_paginator_view(None, pages))

                        else:
                            await send_ephemeral_once(content=f"ℹ️ Action '{at}' not handled here.")

                    if not responded:
                        await send_ephemeral_once(content="✅ Done." if posted_to_channel else "ℹ️ Nothing to do.")
                except Exception as e:
                    await send_ephemeral_once(content=f"❌ Error: {e}")
            return callback


        incoming_names = {(c.get("name") or "").strip() for c in commands if c.get("name")}
        incoming_names.discard("")
        builtin = {"pickflow"}

        # 1) Remove previously managed (both guild and global)
        old_managed = set(self._api_cmd_names.get(gid, set()))
        removed_global = False
        for name in old_managed:
            if name and name not in builtin:
                self.tree.remove_command(name, type=discord.AppCommandType.chat_input, guild=guild)
                self.tree.remove_command(name, type=discord.AppCommandType.chat_input, guild=None)
                removed_global = True

        # 2) Safety pruning of anything stale
        try:
            existing_guild = await self.tree.fetch_commands(guild=guild)
        except Exception:
            existing_guild = []
        try:
            existing_global = await self.tree.fetch_commands()
        except Exception:
            existing_global = []

        for ec in existing_guild:
            if (ec.name not in incoming_names) and (ec.name not in builtin):
                self.tree.remove_command(ec.name, type=ec.type, guild=guild)
            elif (ec.name in incoming_names) and (ec.name not in builtin):
                self.tree.remove_command(ec.name, type=ec.type, guild=guild)

        for ec in existing_global:
            if (ec.name not in incoming_names) and (ec.name not in builtin):
                self.tree.remove_command(ec.name, type=ec.type, guild=None)
                removed_global = True

        # 3) Add fresh (IMPORTANT: pass the WHOLE command spec into the callback)
        for cdef in commands:
            name = (cdef.get("name") or "").strip()
            if not name:
                continue
            desc = (cdef.get("description") or "Custom command")[:100]
            self.tree.add_command(
                app_commands.Command(name=name, description=desc, callback=make_callback(cdef)),
                guild=guild,
            )

        # 4) Track & sync (guild-scoped is instant). If any globals removed, also global sync.
        self._api_cmd_names[gid] = set(incoming_names)
        await self.tree.sync(guild=guild)
        if removed_global:
            try:
                await self.tree.sync()
            except Exception as e:
                print(f"[sync] Global sync failed: {e}")


    async def _clean_old_global_commands(self, names: set[str]):
        """Remove leftover global commands like /runflow, /testflow, etc."""
        try:
            existing_global = await self.tree.fetch_commands()
        except Exception:
            existing_global = []
        removed = False
        for ec in existing_global:
            if ec.name in names:
                self.tree.remove_command(ec.name, type=ec.type, guild=None)
                removed = True
        if removed:
            try:
                await self.tree.sync()
                print("[cleanup] removed legacy globals:", ", ".join(names))
            except Exception as e:
                print("[cleanup] global sync failed:", e)

         
    # ---------- Events ----------
    async def on_member_join(self, m: discord.Member):
        e = self.engines.get(m.guild.id)
        if e:
            await e.handle_member_join(m)

    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        e = self.engines.get(message.guild.id)
        if e:
            await e.handle_message(message)


async def handle_reload(request: web.Request):
    """
    Reload endpoint used by the dashboard Publish action.

    Accepts JSON (preferred) or form/query/plain text:
      {"guild_id":"...", "type":"commands"|"flows"}

    - Tolerates empty / non-JSON bodies (no JSONDecodeError).
    - Returns helpful 4xx errors if inputs are missing/invalid.
    - Runs your existing flows/commands reload paths.
    """
    import os, json
    from urllib.parse import parse_qs
    import discord  # safe to import here as well

    data = {}

    # ---- tolerant parsing (don't explode on non-JSON) ----
    try:
        if request.can_read_body:
            ct = (request.headers.get("Content-Type") or "").lower()
            if "application/json" in ct:
                data = await request.json()
            elif "application/x-www-form-urlencoded" in ct:
                data = dict(await request.post())
            else:
                raw = (await request.text()).strip()
                if raw:
                    # try JSON first
                    try:
                        data = json.loads(raw)
                    except Exception:
                        # fallback: parse querystring style "a=1&b=2"
                        data = {k: (v[0] if isinstance(v, list) else v)
                                for k, v in parse_qs(raw).items()}
    except Exception as e:
        # log but don't 500
        print("[/reload] body parse error:", repr(e))

    # also accept query params if body was empty
    if not data:
        data = {**request.query}

    gid_raw = (data.get("guild_id") or "").strip()
    kind = (data.get("type") or "commands").lower()

    if not gid_raw:
        return web.json_response(
            {"ok": False, "error": "missing guild_id (provide in JSON/form/query)"},
            status=400
        )

    try:
        gid = int(gid_raw)
    except Exception:
        return web.json_response({"ok": False, "error": "invalid guild_id"}, status=400)

    client: MyClient = request.app.get("client")
    if not client:
        return web.json_response({"ok": False, "error": "bot client not attached"}, status=500)

    # ---------------- flows path ----------------
    if kind == "flows":
        try:
            nf = await fetch_guild_flows(gid)
            eng = client.engines.get(gid)
            if eng:
                eng.set_flows(nf)
                await eng.restart_schedules()
            print(f"[reload] flows reloaded for guild {gid}")
            return web.json_response({"ok": True, "type": "flows", "guild_id": str(gid)})
        except Exception as ex:
            print(f"[reload] flows reload failed for guild {gid}: {ex!r}")
            return web.json_response({"ok": False, "error": "flows reload failed"}, status=500)

    # --------------- commands path --------------
    if kind == "commands":
        try:
            cmds = await fetch_guild_commands(gid)
            await client._register_api_commands(gid, cmds.get("commands", []))
            try:
                await client.tree.sync(guild=discord.Object(id=gid))  # guild-scoped = instant
            except Exception as ex:
                print(f"[reload] sync failed for guild {gid}: {ex}")
            print(f"[reload] commands reloaded for guild {gid}")
            return web.json_response({"ok": True, "type": "commands", "guild_id": str(gid)})
        except Exception as ex:
            print(f"[reload] commands reload failed for guild {gid}: {ex!r}")
            return web.json_response({"ok": False, "error": "commands reload failed"}, status=500)

    return web.json_response({"ok": False, "error": "unknown type (use 'commands' or 'flows')"}, status=400)



async def start_webhook_server(client: "MyClient"):
    app = web.Application()
    app["client"] = client
    app.add_routes([web.post("/reload", handle_reload)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, os.getenv("WEBHOOK_HOST", "0.0.0.0"), int(os.getenv("WEBHOOK_PORT", "5001")))
    await site.start()



def main():
    s = get_settings()  # still gets DISCORD_BOT_TOKEN; GUILD_ID not required now
    client = MyClient(intents=intents)
    client.run(s.token)


if __name__ == "__main__":
    main()
