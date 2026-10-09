"""
cogs/rank_sync.py — keeps in-game ER:LC Server Moderator / Server
Administrator rank in sync with Discord staff roles.

Rules (all evaluated independently, not as an elif chain — a member can
match more than one at once, e.g. being demoted from admin to mod):

  1. Has the Moderation Team role, but is NOT a Server Moderator in-game
     -> run `:mod {robloxId}`
  2. IS a Server Moderator in-game, but does NOT have the Moderation
     Team role -> run `:unmod {robloxId}`
  3. Has any of the Directive/Management/Internal Affairs/Administration
     roles, but is NOT a Server Administrator in-game
     -> run `:admin {robloxId}`
  4. IS a Server Administrator in-game, but does NOT have any of those
     roles -> run `:unadmin {robloxId}` immediately followed by
     `:unmod {robloxId}`

Triggers: a member joining the server, or gaining/losing any of the
tracked roles. Only ever runs while the ER:LC server is online (has at
least one player) — matches the "online" definition used elsewhere in
this bot, since ER:LC auto-shuts-down an empty server and in-game
commands can't be sent to a server that isn't live anyway.

"Current in-game rank" is read from the persistent Staff roster
(Admins/Mods, keyed by Roblox ID) via erlc_staff_roster() rather than
Players[].Permission — the roster reflects rank whether or not the
person is actually connected right now, which Players[] does not.

Roblox ID resolution goes through the same Melonly verification API
already used elsewhere in this bot (see cogs/general.py's /rid and
-update commands).
"""
import aiohttp
import discord
from discord.ext import commands

from configs.main import (
    GUILD_ID,
    MELONLY_API_KEY,
    MODERATION_ROLE_ID,
    ADMINISTRATION_ROLE_ID,
    INTERNAL_AFFAIRS_ROLE_ID,
    MANAGEMENT_TEAM_ROLE_ID,
    DIRECTIVE_TEAM_ROLE_ID,
)
from utils.helpers import erlc_players, erlc_run_command, erlc_staff_roster

ADMIN_ROLE_IDS = {
    ADMINISTRATION_ROLE_ID,
    INTERNAL_AFFAIRS_ROLE_ID,
    MANAGEMENT_TEAM_ROLE_ID,
    DIRECTIVE_TEAM_ROLE_ID,
}
TRACKED_ROLE_IDS = {MODERATION_ROLE_ID} | ADMIN_ROLE_IDS

MELONLY_ROBLOX_URL = "https://api.melonly.xyz/api/v1/verification/discord/{discord_id}/roblox"


class RankSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # =========================================================
    # ROBLOX ID RESOLUTION
    # =========================================================
    async def _resolve_roblox_id(self, discord_id: int) -> str | None:
        url = MELONLY_ROBLOX_URL.format(discord_id=discord_id)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {MELONLY_API_KEY}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
        except (aiohttp.ClientError, ValueError):
            return None
        roblox_id = data.get("robloxId") if isinstance(data, dict) else None
        return str(roblox_id) if roblox_id else None

    # =========================================================
    # CORE SYNC
    # =========================================================
    async def _sync_member_rank(self, member: discord.Member):
        if member.bot:
            return

        # In-game commands can only be sent to a live server, and ER:LC
        # auto-shuts-down an empty one — so "online" here means the same
        # thing it means everywhere else in this bot: at least 1 player.
        players = await erlc_players()
        if not players:
            return

        roblox_id = await self._resolve_roblox_id(member.id)
        if not roblox_id:
            return

        roster = await erlc_staff_roster()
        if roster is None:
            # Couldn't read the roster (rate limited, transient error,
            # etc.) — don't guess, just skip this cycle. The next join or
            # role change will try again.
            return

        is_admin_ingame = roblox_id in (roster.get("Admins") or {})
        is_mod_ingame = roblox_id in (roster.get("Mods") or {})

        member_role_ids = {r.id for r in member.roles}
        has_mod_role = MODERATION_ROLE_ID in member_role_ids
        has_admin_role = bool(member_role_ids & ADMIN_ROLE_IDS)

        if has_mod_role and not is_mod_ingame:
            await self._run(member, f":mod {roblox_id}")
        if is_mod_ingame and not has_mod_role:
            await self._run(member, f":unmod {roblox_id}")

        if has_admin_role and not is_admin_ingame:
            await self._run(member, f":admin {roblox_id}")
        if is_admin_ingame and not has_admin_role:
            await self._run(member, f":unadmin {roblox_id}")
            await self._run(member, f":unmod {roblox_id}")

    async def _run(self, member: discord.Member, command: str):
        status, body = await erlc_run_command(command)
        if status != 200:
            print(f"[RANK SYNC] {member} ({member.id}): `{command}` failed — HTTP {status} {body}")
        else:
            print(f"[RANK SYNC] {member} ({member.id}): ran `{command}`")

    # =========================================================
    # LISTENERS
    # =========================================================
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != GUILD_ID:
            return
        await self._sync_member_rank(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if after.guild.id != GUILD_ID or after.bot:
            return

        before_tracked = {r.id for r in before.roles} & TRACKED_ROLE_IDS
        after_tracked = {r.id for r in after.roles} & TRACKED_ROLE_IDS
        if before_tracked == after_tracked:
            return  # nothing relevant to this sync changed

        await self._sync_member_rank(after)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankSync(bot))
