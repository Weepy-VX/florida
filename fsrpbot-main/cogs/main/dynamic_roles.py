"""
cogs/dynamic_roles.py — dynamic warning/strike roling.
Handles automatic role progression when warnings/strikes are issued.
"""
import discord
from datetime import datetime
from discord import app_commands
from discord.ext import commands
from utils.helpers import is_senior_hr, is_ia, err_embed, ok_embed
from configs.main import (
    WARNING_1_ROLE_ID, WARNING_2_ROLE_ID,
    STRIKE_1_ROLE_ID,  STRIKE_2_ROLE_ID,
    COLOUR_BRAND,
)


# =============================================================
# DYNAMIC ROLING LOGIC
# =============================================================

async def apply_dynamic_role(
    member: discord.Member,
    punishment: str,
    cfg=None,
) -> str | None:
    """
    Apply dynamic warning/strike roles based on the infraction type.
    Returns a string describing what changed, or None if no role change.
    Called by the infraction system when a Warning or Strike is issued.
    """
    guild = member.guild
    member_role_ids = {r.id for r in member.roles}

    # Use config from caller if provided (dept bots), else fall back to main config
    _w1 = getattr(cfg, "WARNING_1_ROLE_ID", WARNING_1_ROLE_ID) or WARNING_1_ROLE_ID
    _w2 = getattr(cfg, "WARNING_2_ROLE_ID", WARNING_2_ROLE_ID) or WARNING_2_ROLE_ID
    _s1 = getattr(cfg, "STRIKE_1_ROLE_ID",  STRIKE_1_ROLE_ID)  or STRIKE_1_ROLE_ID
    _s2 = getattr(cfg, "STRIKE_2_ROLE_ID",  STRIKE_2_ROLE_ID)  or STRIKE_2_ROLE_ID

    w1 = guild.get_role(_w1)
    w2 = guild.get_role(_w2)
    s1 = guild.get_role(_s1)
    s2 = guild.get_role(_s2)

    try:
        if punishment == "Warning":
            has_w1 = WARNING_1_ROLE_ID in member_role_ids
            has_w2 = WARNING_2_ROLE_ID in member_role_ids

            if has_w1 and has_w2:
                # Two warnings → convert to Strike 1, remove both warnings
                to_remove = [r for r in [w1, w2] if r]
                to_add    = [r for r in [s1] if r]
                if to_remove:
                    await member.remove_roles(*to_remove, reason="Dynamic role: 2 warnings → Strike 1")
                if to_add:
                    await member.add_roles(*to_add, reason="Dynamic role: 2 warnings → Strike 1")
                return "Converted 2 warnings to **Strike 1**. Warning 1 and Warning 2 removed."

            elif has_w1 and not has_w2:
                # Already has Warning 1 → give Warning 2
                if w2:
                    await member.add_roles(w2, reason="Dynamic role: Warning 2")
                return "**Warning 2** applied."

            else:
                # No warnings yet → give Warning 1
                if w1:
                    await member.add_roles(w1, reason="Dynamic role: Warning 1")
                return "**Warning 1** applied."

        elif punishment == "Strike":
            has_s1 = STRIKE_1_ROLE_ID in member_role_ids
            has_s2 = STRIKE_2_ROLE_ID in member_role_ids

            if has_s2:
                # Already on Strike 2 — flag for demotion consideration, no new role
                return (
                    "⚠️ Member already has **Strike 2**. "
                    "Consider issuing a **Demotion** infraction."
                )
            elif has_s1:
                # Strike 1 → give Strike 2
                if s2:
                    await member.add_roles(s2, reason="Dynamic role: Strike 2")
                return "**Strike 2** applied."
            else:
                # No strikes → give Strike 1
                if s1:
                    await member.add_roles(s1, reason="Dynamic role: Strike 1")
                return "**Strike 1** applied."
    except (discord.Forbidden, discord.HTTPException):
        return "⚠️ Role update could not be applied because the bot lacks permission to edit roles."

    return None


# =============================================================
# COG
# =============================================================

class DynamicRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

async def setup(bot: commands.Bot):
    await bot.add_cog(DynamicRoles(bot))
