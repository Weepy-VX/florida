"""
cogs/erlc.py — ER:LC command runner and server info.
Session management lives in cogs/main/sessions.py.

Uses the official ER:LC Private Server API (https://apidocs.erlc.gg).
Every request requires the `server-key` header. If you're running a
public/multi-server app you also send your app's Global API Key in the
`Authorization` header — see https://apidocs.erlc.gg/creating-public-applications
"""
import asyncio
import logging

import aiohttp

import discord
from discord import app_commands
from discord.ext import commands

from configs.main import COLOUR_BRAND, ERLC_SERVER_KEY, ERLC_GLOBAL_API_KEY
from utils.helpers import err_embed, is_hr, ok_embed
from utils.logger import log_erlc_command

log = logging.getLogger("erlc")

ERLC_BASE = "https://api.erlc.gg/v2/server"

# How long to wait for the ER:LC API before giving up. Without this,
# a hung/unreachable API can leave a command "thinking" indefinitely
# since aiohttp has no timeout by default.
_ERLC_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)

_ERLC_HEADERS = {
    "server-key": ERLC_SERVER_KEY,
    "Accept": "application/json",
}
# Global API key is only needed for public apps hitting many servers.
# Omit it entirely if you're just running this against your own server.
if ERLC_GLOBAL_API_KEY:
    _ERLC_HEADERS["Authorization"] = ERLC_GLOBAL_API_KEY

# ER:LC API error codes -> human-readable messages
# https://apidocs.erlc.gg/error-codes
_ERLC_ERROR_MESSAGES = {
    0: "An unknown error occurred with the ER:LC API.",
    1001: "Error communicating with Roblox or the in-game private server.",
    1002: "An internal ER:LC API system error occurred.",
    2000: "No server-key was provided to the ER:LC API.",
    2001: "The server-key is incorrectly formatted.",
    2002: "The server-key is invalid or expired.",
    2003: "The global API key is invalid.",
    2004: "This server-key is currently banned from accessing the ER:LC API.",
    3001: "No valid command was provided in the request.",
    3002: "The server is currently offline (no players in-game).",
    4000: "Not authorized to perform this action on this server.",
    4001: "Rate limited by the ER:LC API — try again shortly.",
    4002: "That command is restricted.",
    4003: "That message is prohibited.",
    9998: "That resource is restricted.",
    9999: "The in-game ER:LC module is out of date. Kick all players and try again.",
}


def _normalize_command(command: str) -> str:
    """Allow users to omit the leading ':' — 'pm weepy hi' == ':pm weepy hi'."""
    command = command.strip()
    if command.startswith(":"):
        command = command[1:].lstrip()
    return f":{command}"


def _is_rate_limited(status: int, payload: dict | None) -> bool:
    code = payload.get("code") if isinstance(payload, dict) else None
    return status == 429 or code == 4001


def _erlc_error_message(status: int, payload: dict | None, retry_after: str | None = None) -> str:
    code = None
    if isinstance(payload, dict):
        code = payload.get("code")

    # Status-based special cases take priority — the API doesn't always
    # include a `code` in the body for these.
    if status == 422 or code == 3002:
        return "The private server is currently offline (no players in-game)."
    if status == 429 or code == 4001:
        if retry_after:
            return f"Being rate limited by the ER:LC API. Try again in {retry_after}s."
        return "Being rate limited by the ER:LC API — try again shortly."
    if code in _ERLC_ERROR_MESSAGES:
        return _ERLC_ERROR_MESSAGES[code]
    if status == 403:
        return "Unauthorized — check your server-key / global API key."
    if isinstance(payload, dict) and payload.get("message"):
        return payload["message"]
    if isinstance(payload, dict) and payload.get("error"):
        return payload["error"]
    return f"Failed to reach the ER:LC API (HTTP {status})."


# =============================================================
# Cog
# =============================================================

class ERLC(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_ERLC_TIMEOUT)
        return self._session

    async def _erlc_get(self, path: str = "", params: dict | None = None):
        """GET helper against the ER:LC API. Returns (ok, data_or_error_message)."""
        session = await self._get_session()
        try:
            async with session.get(f"{ERLC_BASE}{path}", headers=_ERLC_HEADERS, params=params) as resp:
                try:
                    payload = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    payload = None
                if resp.status != 200:
                    message = _erlc_error_message(resp.status, payload, resp.headers.get("Retry-After"))
                    log.error("ER:LC GET %s%s failed [%s]: %s", ERLC_BASE, path, resp.status, payload)
                    return False, message
                return True, payload
        except asyncio.TimeoutError:
            log.error("ER:LC GET %s%s timed out after %ss", ERLC_BASE, path, _ERLC_TIMEOUT.total)
            return False, "The ER:LC API did not respond in time — it may be down."
        except aiohttp.ClientConnectorError as e:
            log.error("ER:LC GET %s%s connection failed: %s", ERLC_BASE, path, e)
            return False, "Could not connect to the ER:LC API — it may be down."
        except aiohttp.ClientError as e:
            log.error("ER:LC GET %s%s failed: %s", ERLC_BASE, path, e)
            return False, "Something went wrong talking to the ER:LC API."

    async def _erlc_post_command(self, command: str):
        """POST a virtual server-management command. Returns (ok, message, rate_limited)."""
        session = await self._get_session()
        try:
            async with session.post(
                f"{ERLC_BASE}/command",
                headers=_ERLC_HEADERS,
                json={"command": command},
            ) as resp:
                try:
                    payload = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    payload = None
                if resp.status != 200:
                    message = _erlc_error_message(resp.status, payload, resp.headers.get("Retry-After"))
                    log.error("ER:LC POST %s/command failed [%s]: %s (command=%r)", ERLC_BASE, resp.status, payload, command)
                    return False, message, _is_rate_limited(resp.status, payload)
                return True, (payload or {}).get("message", "Command executed successfully."), False
        except asyncio.TimeoutError:
            log.error("ER:LC POST %s/command timed out after %ss (command=%r)", ERLC_BASE, _ERLC_TIMEOUT.total, command)
            return False, "The ER:LC API did not respond in time — it may be down.", False
        except aiohttp.ClientConnectorError as e:
            log.error("ER:LC POST %s/command connection failed: %s (command=%r)", ERLC_BASE, e, command)
            return False, "Could not connect to the ER:LC API — it may be down.", False
        except aiohttp.ClientError as e:
            log.error("ER:LC POST %s/command failed: %s (command=%r)", ERLC_BASE, e, command)
            return False, "Something went wrong talking to the ER:LC API.", False

    @commands.hybrid_command(name="erlc-info", description="View current ER:LC server information.")
    async def erlc_info(self, ctx: commands.Context):
        await ctx.defer()
        ok, data = await self._erlc_get(params={"Queue": "true"})
        payload = data if isinstance(data, dict) else {}
        if not ok:
            message_text = str(data or "").lower()
            if "offline" in message_text or "no players" in message_text:
                embed = discord.Embed(
                    title="Server Offline",
                    description="The ER:LC private server is currently offline with no players in-game.",
                    color=discord.Color.orange(),
                )
                if ctx.interaction is not None:
                    await ctx.interaction.followup.send(embed=embed)
                else:
                    await ctx.send(embed=embed)
            else:
                if ctx.interaction is not None:
                    await ctx.interaction.followup.send(embed=err_embed(f"Failed to get ER:LC server information. {data}"), ephemeral=True)
                else:
                    await ctx.send(embed=err_embed(f"Failed to get ER:LC server information. {data}"), ephemeral=True)
            return

        queue_count = len(payload.get("Queue", []) or [])
        embed = discord.Embed(
            title="<:fla:1531440112183804045> FSRP Server Info",
            color=COLOUR_BRAND,
            description=(
                f"> **Server Name:** {payload.get('Name', 'N/A')}\n"
                f"> **Join Code:** {payload.get('JoinKey', 'N/A')}\n"
                f"> **Current Players:** {payload.get('CurrentPlayers', 'N/A')}/{payload.get('MaxPlayers', 'N/A')}\n"
                f"> **Queue:** {queue_count}"
            ),
        )
        embed.set_thumbnail(url="https://i.imgur.com/9KCE36G.png")
        embed.set_image(url="https://i.imgur.com/e72iEAY.png")
        if ctx.interaction is not None:
            await ctx.interaction.followup.send(embed=embed)
        else:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="command", description="Run an ER:LC in-game command as virtual server management.")
    @app_commands.describe(command="The ER:LC command to run, e.g. h Hello everyone! (leading ':' optional)")
    async def erlc_command(self, ctx: commands.Context, *, command: str):
        if not is_hr(ctx.author):
            await ctx.reply(embed=err_embed("You do not have permission to run ER:LC commands."), ephemeral=True)
            return

        normalized = _normalize_command(command)

        # Ack immediately so Discord doesn't sit on "thinking...", then show
        # a visible "Sending" message that we edit in place once the ER:LC
        # API responds. ctx.send() is used (rather than ctx.followup.send)
        # because it reliably hands back a real discord.Message for both
        # prefix and slash invocation once the interaction is deferred.
        await ctx.defer()
        sending_embed = discord.Embed(
            description=f"📡 Sending `{normalized}`...",
            color=COLOUR_BRAND,
        )
        try:
            status_msg = await ctx.send(embed=sending_embed)
            print(f"[erlc] sent 'Sending' status message for {normalized!r}")
        except discord.HTTPException:
            log.exception("Failed to send 'Sending' status message for ER:LC command %r", normalized)
            print(f"[erlc] FAILED to send 'Sending' status message for {normalized!r}")
            return

        try:
            print(f"[erlc] calling ER:LC API for {normalized!r}")
            ok, message, rate_limited = await asyncio.wait_for(
                self._erlc_post_command(normalized), timeout=15
            )
            print(f"[erlc] ER:LC API returned ok={ok} rate_limited={rate_limited} for {normalized!r}")
        except asyncio.TimeoutError:
            log.error("ER:LC command %r did not resolve within 15s (hard timeout)", normalized)
            print(f"[erlc] command {normalized!r} hard-timed-out after 15s")
            try:
                await status_msg.edit(embed=err_embed("The ER:LC command handler timed out unexpectedly."))
            except discord.HTTPException:
                log.exception("Failed to edit status message after hard timeout for %r", normalized)
            return
        except Exception:
            log.exception("Unexpected error running ER:LC command %r", normalized)
            print(f"[erlc] unexpected error running command {normalized!r}")
            try:
                await status_msg.edit(embed=err_embed("An unexpected error occurred while running that command."))
            except discord.HTTPException:
                log.exception("Failed to edit status message after unexpected error for %r", normalized)
            return

        try:
            if rate_limited:
                result_embed = discord.Embed(
                    title="Rate Limited",
                    description=f"⏳ {message}",
                    color=discord.Color.orange(),
                )
                await status_msg.edit(embed=result_embed)
            elif not ok:
                await status_msg.edit(embed=err_embed(f"Failed to run command. {message}"))
            else:
                await status_msg.edit(embed=ok_embed(f"Ran command `{normalized}`.\n{message}"))
        except discord.HTTPException:
            log.exception("Failed to edit status message with result for ER:LC command %r", normalized)
            print(f"[erlc] failed to edit status message for {normalized!r}")
            return

        # Logging happens last and can't block or hide the user-visible result above.
        if ok and not rate_limited:
            try:
                await asyncio.wait_for(log_erlc_command(ctx.author, normalized), timeout=10)
            except asyncio.TimeoutError:
                log.error("log_erlc_command timed out for %r", normalized)
                print(f"[erlc] log_erlc_command timed out for {normalized!r}")
            except Exception:
                log.exception("log_erlc_command failed for %r", normalized)
                print(f"[erlc] log_erlc_command failed for {normalized!r}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ERLC(bot))