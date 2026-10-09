"""
utils/logger.py — Centralised bot activity logger.
Posts structured embeds to the bot log channel for every significant action.
"""
import discord
from datetime import datetime
from configs.main import COLOUR_BRAND, BOT_LOG_CHANNEL_ID


async def _send(bot, embed: discord.Embed, guild_id: int = None):
    """Find the log channel and send the embed."""
    # Try the configured log channel first
    ch = bot.get_channel(BOT_LOG_CHANNEL_ID)
    if not ch and guild_id:
        guild = bot.get_guild(guild_id)
        if guild:
            ch = guild.get_channel(BOT_LOG_CHANNEL_ID)
    if ch:
        try:
            await ch.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass


def _base(title: str, colour: int = COLOUR_BRAND) -> discord.Embed:
    e = discord.Embed(title=title, color=colour, timestamp=datetime.utcnow())
    return e


# =============================================================
# MODERATION
# =============================================================

async def log_ban(bot, guild: discord.Guild, user: discord.User, reason: str, mirrored_to: list[str] = None):
    e = _base("Member Banned")
    e.add_field(name="User",   value=f"{user} (`{user.id}`)", inline=True)
    e.add_field(name="Server", value=guild.name,              inline=True)
    e.add_field(name="Reason", value=reason,                  inline=False)
    if mirrored_to:
        e.add_field(name="Mirrored to", value="\n".join(mirrored_to), inline=False)
    e.set_thumbnail(url=user.display_avatar.url)
    await _send(bot, e, guild.id)


async def log_unban(bot, guild: discord.Guild, user: discord.User, mirrored_to: list[str] = None):
    e = _base("Member Unbanned")
    e.add_field(name="User",   value=f"{user} (`{user.id}`)", inline=True)
    e.add_field(name="Server", value=guild.name,              inline=True)
    if mirrored_to:
        e.add_field(name="Mirrored to", value="\n".join(mirrored_to), inline=False)
    e.set_thumbnail(url=user.display_avatar.url)
    await _send(bot, e, guild.id)


async def log_kick(bot, guild: discord.Guild, user: discord.User, reason: str, mirrored_to: list[str] = None):
    e = _base("Member Kicked")
    e.add_field(name="User",   value=f"{user} (`{user.id}`)", inline=True)
    e.add_field(name="Server", value=guild.name,              inline=True)
    e.add_field(name="Reason", value=reason,                  inline=False)
    if mirrored_to:
        e.add_field(name="Mirrored to", value="\n".join(mirrored_to), inline=False)
    e.set_thumbnail(url=user.display_avatar.url)
    await _send(bot, e, guild.id)


# =============================================================
# STAFF ACTIONS
# =============================================================

async def log_promotion(bot, guild: discord.Guild, member: discord.Member,
                         promoter: discord.Member, new_role: discord.Role, reason: str):
    e = _base("Staff Promoted")
    e.add_field(name="Member",     value=member.mention,   inline=True)
    e.add_field(name="Promoted by",value=promoter.mention, inline=True)
    e.add_field(name="New role",   value=new_role.mention, inline=True)
    e.add_field(name="Reason",     value=reason,           inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    await _send(bot, e, guild.id)


async def log_resignation(bot, guild: discord.Guild, member: discord.Member,
                           issuer: discord.Member, roles_removed: list[str]):
    e = _base("Staff Resigned")
    e.add_field(name="Member",        value=member.mention,                             inline=True)
    e.add_field(name="Processed by",  value=issuer.mention,                             inline=True)
    e.add_field(name="Roles removed", value=", ".join(roles_removed) or "None",         inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    await _send(bot, e, guild.id)


async def log_mterm(bot, guild: discord.Guild, member: discord.Member,
                    issuer: discord.Member, reason: str, results: list[str]):
    e = _base("Mass Termination")
    e.add_field(name="Target",    value=f"{member} (`{member.id}`)", inline=True)
    e.add_field(name="Issued by", value=issuer.mention,              inline=True)
    e.add_field(name="Reason",    value=reason,                      inline=False)
    e.add_field(name="Results",   value="\n".join(results),          inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    await _send(bot, e, guild.id)


async def log_munban(bot, guild: discord.Guild, user: discord.User,
                     issuer: discord.Member, reason: str, results: list[str]):
    e = _base("Mass Unban")
    e.add_field(name="User",      value=f"{user} (`{user.id}`)", inline=True)
    e.add_field(name="Issued by", value=issuer.mention,          inline=True)
    e.add_field(name="Reason",    value=reason,                  inline=False)
    e.add_field(name="Results",   value="\n".join(results),      inline=False)
    await _send(bot, e, guild.id)


# =============================================================
# INFRACTIONS
# =============================================================

async def log_infraction(bot, guild: discord.Guild, member: discord.Member,
                          issuer: discord.Member, punishment: str, reason: str,
                          case_id: int, role_note: str = None):
    e = _base(f"Infraction Issued — Case #{case_id}")
    e.add_field(name="Member",     value=member.mention,  inline=True)
    e.add_field(name="Issued by",  value=issuer.mention,  inline=True)
    e.add_field(name="Punishment", value=punishment,       inline=True)
    e.add_field(name="Reason",     value=reason,           inline=False)
    if role_note:
        e.add_field(name="Role update", value=role_note,  inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    await _send(bot, e, guild.id)


async def log_infraction_edit(bot, guild: discord.Guild, editor: discord.Member,
                               case_id: int, changes: dict):
    e = _base(f"Infraction Edited — Case #{case_id}")
    e.add_field(name="Edited by", value=editor.mention, inline=True)
    for field, value in changes.items():
        e.add_field(name=field, value=value, inline=True)
    await _send(bot, e, guild.id)


async def log_mass_infract(bot, guild: discord.Guild, issuer: discord.Member,
                            punishment: str, reason: str, count: int, results: list[str]):
    e = _base(f"Mass Infraction — {punishment}")
    e.add_field(name="Issued by", value=issuer.mention,    inline=True)
    e.add_field(name="Count",     value=str(count),         inline=True)
    e.add_field(name="Reason",    value=reason,             inline=False)
    e.add_field(name="Results",   value="\n".join(results[:20]) + ("..." if len(results) > 20 else ""), inline=False)
    await _send(bot, e, guild.id)


# =============================================================
# SESSIONS
# =============================================================

async def log_session_start(bot, guild: discord.Guild, host: discord.Member,
                             kind: str, session_id: int):
    e = _base(f"Session Started — {kind}")
    e.add_field(name="Host",       value=host.mention,   inline=True)
    e.add_field(name="Type",       value=kind,           inline=True)
    e.add_field(name="Session ID", value=f"#{session_id}", inline=True)
    await _send(bot, e, guild.id)


async def log_session_end(bot, guild: discord.Guild, ended_by: discord.Member,
                           kind: str, session_id: int, duration: str):
    e = _base(f"Session Ended — {kind}")
    e.add_field(name="Ended by",   value=ended_by.mention, inline=True)
    e.add_field(name="Duration",   value=duration,         inline=True)
    e.add_field(name="Session ID", value=f"#{session_id}", inline=True)
    await _send(bot, e, guild.id)


# Session boosts are disabled.
# async def log_session_boost(bot, guild: discord.Guild, issuer: discord.Member, session_id: int):
#     e = _base("Session Boost")
#     e.add_field(name="Issued by",  value=issuer.mention,   inline=True)
#     e.add_field(name="Session ID", value=f"#{session_id}", inline=True)
#     await _send(bot, e, guild.id)


# =============================================================
# FEEDBACK
# =============================================================

async def log_feedback(bot, guild: discord.Guild, submitter: discord.Member,
                        target: discord.Member, rating: int, reason: str):
    stars = "⭐" * rating
    e = _base("Feedback Submitted")
    e.add_field(name="For",         value=target.mention,    inline=True)
    e.add_field(name="Submitted by",value=submitter.mention, inline=True)
    e.add_field(name="Rating",      value=stars,             inline=True)
    e.add_field(name="Feedback",    value=reason,            inline=False)
    e.set_thumbnail(url=target.display_avatar.url)
    await _send(bot, e, guild.id)


# =============================================================
# SUSPEND / UNSUSPEND
# =============================================================

async def log_suspend(bot, guild: discord.Guild, member: discord.Member,
                       issuer: discord.Member, reason: str):
    e = _base("Member Suspended")
    e.add_field(name="Member",    value=member.mention, inline=True)
    e.add_field(name="Issued by", value=issuer.mention, inline=True)
    e.add_field(name="Reason",    value=reason,         inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    await _send(bot, e, guild.id)


async def log_unsuspend(bot, guild: discord.Guild, member: discord.Member,
                         issuer: discord.Member, reason: str, restored: int):
    e = _base("Member Unsuspended")
    e.add_field(name="Member",         value=member.mention, inline=True)
    e.add_field(name="Issued by",      value=issuer.mention, inline=True)
    e.add_field(name="Roles restored", value=str(restored),  inline=True)
    e.add_field(name="Reason",         value=reason,         inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    await _send(bot, e, guild.id)


# =============================================================
# ERLC COMMANDS
# =============================================================

async def log_erlc_command(bot, guild: discord.Guild, issuer: discord.Member, cmd: str, status: int):
    e = _base("ER:LC Command Run")
    e.add_field(name="Issued by", value=issuer.mention,                          inline=True)
    e.add_field(name="Command",   value=f"`{cmd}`",                              inline=True)
    e.add_field(name="Status",    value=f"`{status}` {'✅' if status in (200,204) else '❌'}", inline=True)
    await _send(bot, e, guild.id)


# =============================================================
# COMMANDS RUN (general audit)
# =============================================================

async def log_command(bot, ctx_or_interaction, command_name: str):
    """Generic log for any command being run."""
    if hasattr(ctx_or_interaction, 'guild') and ctx_or_interaction.guild:
        guild_id  = ctx_or_interaction.guild.id
        guild_name = ctx_or_interaction.guild.name
    else:
        return
    user = getattr(ctx_or_interaction, 'author', None) or getattr(ctx_or_interaction, 'user', None)
    e = _base("Command Used")
    e.add_field(name="Command", value=f"`{command_name}`",        inline=True)
    e.add_field(name="User",    value=f"{user} (`{user.id}`)",    inline=True)
    e.add_field(name="Server",  value=guild_name,                 inline=True)
    await _send(bot, e, guild_id)
