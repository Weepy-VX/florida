"""
cogs/infractions.py — Staff infraction system.

Commands (slash only, grouped under /infractions):
  /infractions issue      — Issue a staff infraction
  /infractions current    — Active Warnings/Strikes only
  /infractions view       — Full case history
  /infractions edit       — Edit punishment/reason/status via modal; updates log + DM
  /infractions mass       — Bulk issue via UserSelect (up to 25 members)
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from cogs.main.dynamic_roles import apply_dynamic_role
from configs.main import (
    COLOUR_BRAND,
    EMBED_IMAGE_INFRACTION,
    INFRACTION_ISSUER_ROLE_IDS,
    INFRACTION_LOG_CHANNEL_ID,
    INFRACTION_TYPES,
)
from utils.db import (
    create_infraction,
    edit_infraction as db_edit_infraction,
    get_active_infractions,
    get_all_infractions,
    update_infraction_log,
    save_suspension,
    load_suspension,
    clear_suspension,
)
from utils.helpers import err_embed, info_embed, ok_embed

# ── Constants ──────────────────────────────────────────────────

STATUS_OPTIONS = ("Active", "Voided", "Appealed")

# Suspension system
SUSPENDED_ROLE_ID = 1532632204016881745
SUSPENSION_LOG_CHANNEL_ID = 1531704535473586326


# ── Helpers ────────────────────────────────────────────────────

def _can_issue(member: discord.Member) -> bool:
    ids = {r.id for r in member.roles}
    return bool(ids & set(INFRACTION_ISSUER_ROLE_IDS))


def _build_infraction_embed(
    *,
    punishment: str,
    reason: str,
    case_id: int,
    issuer: discord.Member | None = None,
    issuer_mention: str = "*Unknown*",
    status: str = "Active",
    created_at: str | None = None,
    member: discord.Member | None = None,
    member_id: int | None = None,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    colour = COLOUR_BRAND

    # Resolve timestamp
    ts = datetime.utcnow()
    if created_at:
        try:
            ts = datetime.fromisoformat(created_at)
        except Exception:
            pass

    # Resolve mention strings
    if issuer:
        issuer_mention = issuer.mention
    if member:
        member_val = member.mention
    elif member_id:
        member_val = f"<@{member_id}>"
    else:
        member_val = "*Unknown*"

    embed = discord.Embed(
        title="Staff Infraction",
        description=(
            f"Your account has received an infraction for the following reason(s). "
            f"If you believe this was a mistake or would like to appeal, please submit an appeal. "
            f"This infraction was issued by {issuer_mention}, "
            f"following Disciplinary Action {case_id};"
        ),
        color=colour,
        timestamp=ts,
    )

    # Author: issuer name + avatar
    if issuer:
        embed.set_author(name=f"Issued by {issuer.display_name}", icon_url=issuer.display_avatar.url)
    elif issuer_mention != "*Unknown*":
        embed.set_author(name="Staff Infraction")

    # Thumbnail: guild icon
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    # Fields: 2-column layout matching the screenshot
    embed.add_field(name="Member",     value=member_val,   inline=True)
    embed.add_field(name="Punishment", value=punishment,   inline=True)
    embed.add_field(name="Issuer",     value=issuer_mention, inline=True)
    embed.add_field(name="Reason",     value=reason,       inline=True)

    # Banner image
    if EMBED_IMAGE_INFRACTION:
        embed.set_image(url=EMBED_IMAGE_INFRACTION)

    # Footer with status + Discord auto-timestamp
    embed.set_footer(text=f"Case #{case_id}  •  Status: {status}")

    return embed



async def _issue_one(
    *,
    guild: discord.Guild,
    member: discord.Member,
    punishment: str,
    reason: str,
    issuer: discord.Member,
    log_ch: discord.TextChannel,
) -> tuple[int, discord.Message | None, bool]:
    """
    Save one infraction to the DB, post the log embed, and DM the member.
    Returns (case_id, log_message, dm_sent).
    """
    case_id = create_infraction(
        guild.id, member.id, issuer.id, punishment, reason
    )

    # Dynamic role updates for Warnings/Strikes now run in the background
    # (fire-and-forget) instead of being awaited inline. They're no longer
    # surfaced in the infraction embed or the command response — this just
    # keeps issuing an infraction fast and decoupled from role syncing.
    if punishment in ("Warning", "Strike"):
        asyncio.create_task(apply_dynamic_role(member=member, punishment=punishment))

    embed = _build_infraction_embed(
        punishment=punishment,
        reason=reason,
        case_id=case_id,
        issuer=issuer,
        member=member,
        guild=guild,
    )

    log_msg: discord.Message | None = None
    try:
        # Pass the member's mention into the message content so Discord registers the ping
        log_msg = await log_ch.send(content=member.mention, embed=embed)
    except Exception:
        pass

    dm_ok  = False
    dm_msg: discord.Message | None = None
    try:
        dm_embed = _build_infraction_embed(
            punishment=punishment,
            reason=reason,
            case_id=case_id,
            issuer=issuer,
            member=member,
            guild=guild,
        )
        dm = await member.create_dm()
        dm_msg = await dm.send(
            content=f"You have received a **{punishment}** in **{guild.name}**.",
            embed=dm_embed,
        )
        dm_ok = True
    except discord.Forbidden:
        pass
    except Exception:
        pass

    update_infraction_log(
        case_id,
        log_msg.channel.id if log_msg else None,
        log_msg.id         if log_msg else None,
        dm_msg.channel.id  if dm_msg else None,
        dm_msg.id          if dm_msg else None,
    )
    return case_id, log_msg, dm_ok


# ── Views ──────────────────────────────────────────────────────


class MassInfractView(discord.ui.View):
    def __init__(
        self,
        *,
        punishment: str,
        reason: str,
        issuer: discord.Member,
        log_ch: discord.TextChannel,
    ):
        super().__init__(timeout=180)
        self.punishment = punishment
        self.reason     = reason
        self.issuer     = issuer
        self.log_ch     = log_ch
        self.selected: list[discord.Member] = []

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Select members to infract (up to 25)",
        min_values=1,
        max_values=25,
    )
    async def user_select(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ):
        if interaction.user.id != self.issuer.id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return
        self.selected = [v for v in select.values if isinstance(v, discord.Member)]
        await interaction.response.defer()

    @discord.ui.button(label="Issue to Selected", style=discord.ButtonStyle.success, row=1)
    async def issue(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.issuer.id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return
        if not self.selected:
            await interaction.response.send_message(
                "Select at least one member first.", ephemeral=True
            )
            return
        self.stop()
        await interaction.response.edit_message(
            embed=ok_embed(
                f"Mass {self.punishment} — issuing to {len(self.selected)}…",
                "Please wait…",
            ),
            view=None,
        )

        lines: list[str] = []
        for member in self.selected:
            case_id, log_msg, dm_ok = await _issue_one(
                guild=interaction.guild,
                member=member,
                punishment=self.punishment,
                reason=self.reason,
                issuer=self.issuer,
                log_ch=self.log_ch,
            )
            lines.append(
                f"**{member.mention}** — Case `#{case_id}` · "
                f"Log: {log_msg.jump_url if log_msg else '❌'} · "
                f"DM: {'✅' if dm_ok else '❌'}"
            )

        await interaction.edit_original_response(
            embed=ok_embed(
                f"Mass {self.punishment} — {len(lines)} issued",
                "\n".join(lines),
            ),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.issuer.id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return
        self.stop()
        await interaction.response.edit_message(
            embed=err_embed("Mass infraction cancelled."), view=None
        )




# ── Cog ────────────────────────────────────────────────────────

class Infractions(commands.Cog):
    # All infraction commands now live under a single /infractions group
    # (e.g. /infractions issue, /infractions view) instead of separate
    # top-level infraction-{command} slash commands.
    infractions_group = app_commands.Group(
        name="infractions",
        description="Staff infraction commands.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _guard(self, member: discord.Member) -> bool:
        ids = {r.id for r in member.roles}
        return bool(ids & set(INFRACTION_ISSUER_ROLE_IDS))

    # ── /infractions suspend ────────────────────────────────────

    @infractions_group.command(
        name="suspend",
        description="Suspend a member: strips their roles, applies the Suspended role, and logs it.",
    )
    @app_commands.describe(
        member="Member to suspend",
        reason="Reason for the suspension",
    )
    async def infraction_suspend(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        await interaction.response.defer(ephemeral=True)
        if not self._guard(interaction.user):
            await interaction.followup.send(
                embed=err_embed("You are not authorised to suspend members."),
                ephemeral=True,
            )
            return

        if member.bot or member.id == interaction.user.id:
            await interaction.followup.send(
                embed=err_embed("You cannot suspend this member."), ephemeral=True
            )
            return

        suspended_role = interaction.guild.get_role(SUSPENDED_ROLE_ID)
        if not suspended_role:
            await interaction.followup.send(
                embed=err_embed("Could not find the configured Suspended role."),
                ephemeral=True,
            )
            return

        if suspended_role in member.roles:
            await interaction.followup.send(
                embed=err_embed(f"{member.mention} is already suspended."),
                ephemeral=True,
            )
            return

        # Save every removable role (skips @everyone and managed/integration roles)
        # so it can be restored on unsuspend.
        roles_to_remove = [
            role for role in member.roles
            if role != interaction.guild.default_role and not role.managed
        ]
        save_suspension(
            interaction.guild_id, member.id, [role.id for role in roles_to_remove]
        )

        try:
            if roles_to_remove:
                await member.remove_roles(
                    *roles_to_remove, reason=f"Suspended by {interaction.user}: {reason}"
                )
            await member.add_roles(
                suspended_role, reason=f"Suspended by {interaction.user}: {reason}"
            )
        except discord.Forbidden:
            clear_suspension(interaction.guild_id, member.id)
            await interaction.followup.send(
                embed=err_embed("I don't have permission to modify that member's roles."),
                ephemeral=True,
            )
            return

        log_ch = interaction.guild.get_channel(SUSPENSION_LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="🚫 User Suspended", color=discord.Color.red())
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            embed=ok_embed("Member suspended", f"{member.mention} has been suspended."),
            ephemeral=True,
        )

    # ── /infractions unsuspend ──────────────────────────────────

    @infractions_group.command(
        name="unsuspend",
        description="Restore a suspended member's prior roles.",
    )
    @app_commands.describe(member="Member to unsuspend")
    async def infraction_unsuspend(
        self, interaction: discord.Interaction, member: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)
        if not self._guard(interaction.user):
            await interaction.followup.send(
                embed=err_embed("You are not authorised to unsuspend members."),
                ephemeral=True,
            )
            return

        suspended_role = interaction.guild.get_role(SUSPENDED_ROLE_ID)
        is_suspended = bool(suspended_role and suspended_role in member.roles)

        saved_role_ids = load_suspension(interaction.guild_id, member.id)
        if not saved_role_ids and not is_suspended:
            await interaction.followup.send(
                embed=err_embed(f"{member.mention} is not currently suspended."),
                ephemeral=True,
            )
            return

        roles_to_restore = [
            role for r_id in saved_role_ids
            if (role := interaction.guild.get_role(r_id)) is not None
        ]

        try:
            if is_suspended:
                await member.remove_roles(
                    suspended_role, reason=f"Unsuspended by {interaction.user}"
                )
            if roles_to_restore:
                await member.add_roles(
                    *roles_to_restore, reason=f"Unsuspended by {interaction.user}"
                )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=err_embed("I don't have permission to modify that member's roles."),
                ephemeral=True,
            )
            return

        clear_suspension(interaction.guild_id, member.id)

        log_ch = interaction.guild.get_channel(SUSPENSION_LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="✅ User Unsuspended", color=discord.Color.green())
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            embed=ok_embed(
                "Member unsuspended",
                f"{member.mention} has been unsuspended and their roles have been restored.",
            ),
            ephemeral=True,
        )

    # ── /infractions issue ─────────────────────────────────────

    @infractions_group.command(
        name="issue",
        description="Issue a staff infraction.",
    )
    @app_commands.describe(
        member="Member to infract",
        punishment="Type of punishment",
        reason="Reason for the infraction",
    )
    @app_commands.choices(
        punishment=[app_commands.Choice(name=t, value=t) for t in INFRACTION_TYPES]
    )
    async def infraction_issue(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        punishment: str,
        reason: str,
    ):
        await interaction.response.defer(ephemeral=True)
        if not self._guard(interaction.user):
            await interaction.followup.send(
                embed=err_embed("You are not authorised to issue infractions."),
                ephemeral=True,
            )
            return

        log_ch = interaction.guild.get_channel(INFRACTION_LOG_CHANNEL_ID)
        if not log_ch:
            await interaction.followup.send(
                embed=err_embed("Infraction log channel not configured."), ephemeral=True
            )
            return

        case_id, log_msg, dm_ok = await _issue_one(
            guild=interaction.guild,
            member=member,
            punishment=punishment,
            reason=reason,
            issuer=interaction.user,
            log_ch=log_ch,
        )

        lines = [
            f"**{punishment}** issued to {member.mention}.",
            f"**Case ID:** `#{case_id}`",
            f"**Log:** {log_msg.jump_url if log_msg else '❌ Failed to post'}",
            f"**DM:** {'✅ Sent' if dm_ok else '❌ Could not DM'}",
        ]
        await interaction.followup.send(
            embed=ok_embed(f"Case #{case_id} issued", "\n".join(lines)),
            ephemeral=True,
        )

    # ── /infractions current ──────────────────────────────────

    @infractions_group.command(
        name="current",
        description="View a member's active Warnings and Strikes.",
    )
    @app_commands.describe(member="Member to look up")
    async def current_infractions(
        self, interaction: discord.Interaction, member: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)
        if not self._guard(interaction.user):
            await interaction.followup.send(
                embed=err_embed("You are not authorised to view infractions."),
                ephemeral=True,
            )
            return

        rows = [
            r
            for r in get_active_infractions(interaction.guild_id, member.id)
            if r["punishment"] in ("Warning", "Strike")
        ]
        if not rows:
            await interaction.followup.send(
                embed=ok_embed(
                    "No active warnings/strikes",
                    f"{member.mention} has no active Warnings or Strikes.",
                ),
                ephemeral=True,
            )
            return

        embed = info_embed(
            f"Active Infractions — {member.display_name}",
            f"{len(rows)} active case(s)",
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        for row in rows:
            try:
                ts     = int(datetime.fromisoformat(row["created_at"]).timestamp())
                issued = f"<t:{ts}:f>"
            except Exception:
                issued = row["created_at"]
            embed.add_field(
                name=f"Case #{row['case_id']} — {row['punishment']}",
                value=f"**Reason:** {row['reason']}\n**Issued:** {issued}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /infractions view ─────────────────────────────────────

    @infractions_group.command(
        name="view",
        description="View a member's full infraction history.",
    )
    @app_commands.describe(member="Member to look up")
    async def infractions_view(
        self, interaction: discord.Interaction, member: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)
        if not self._guard(interaction.user):
            await interaction.followup.send(
                embed=err_embed("You are not authorised to view infractions."),
                ephemeral=True,
            )
            return

        rows = get_all_infractions(interaction.guild_id, member.id)
        if not rows:
            await interaction.followup.send(
                embed=ok_embed(
                    "No infractions", f"{member.mention} has no infraction history."
                ),
                ephemeral=True,
            )
            return

        embed = info_embed(
            f"Infraction History — {member.display_name}",
            f"{len(rows)} total case(s)",
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        for row in rows:
            try:
                ts     = int(datetime.fromisoformat(row["created_at"]).timestamp())
                issued = f"<t:{ts}:f>"
            except Exception:
                issued = row["created_at"]
            embed.add_field(
                name=f"Case #{row['case_id']} — {row['punishment']} [{row['status']}]",
                value=f"**Reason:** {row['reason']}\n**Issued:** {issued}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /infractions edit ──────────────────────────────────────

    @infractions_group.command(
        name="edit",
        description="Edit an existing infraction by Case ID.",
    )
    @app_commands.describe(
        case_id="Case ID to edit",
        punishment="New punishment type (leave blank to keep current)",
        reason="New reason (leave blank to keep current)",
        status="New status (leave blank to keep current)",
    )
    @app_commands.choices(
        punishment=[app_commands.Choice(name=t, value=t) for t in INFRACTION_TYPES],
        status=[app_commands.Choice(name=s, value=s) for s in STATUS_OPTIONS],
    )
    async def infraction_edit(
        self,
        interaction: discord.Interaction,
        case_id: int,
        punishment: str | None = None,
        reason: str | None = None,
        status: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not self._guard(interaction.user):
            await interaction.followup.send(
                embed=err_embed("You are not authorised to edit infractions."),
                ephemeral=True,
            )
            return

        updated = db_edit_infraction(
            case_id,
            interaction.guild_id,
            punishment=punishment,
            reason=reason,
            status=status,
        )
        if not updated:
            await interaction.followup.send(
                embed=err_embed(f"Case `#{case_id}` not found in this server."),
                ephemeral=True,
            )
            return

        member   = interaction.guild.get_member(updated["user_id"])
        issuer_m = interaction.guild.get_member(updated["issuer_id"])
        issuer_mention = issuer_m.mention if issuer_m else f"<@{updated['issuer_id']}>"

        new_embed = _build_infraction_embed(
            punishment     = updated["punishment"],
            reason         = updated["reason"],
            case_id        = case_id,
            issuer         = issuer_m,
            issuer_mention = issuer_mention,
            status         = updated["status"],
            created_at     = updated["created_at"],
            member         = member,
            member_id      = updated["user_id"] if not member else None,
            guild          = interaction.guild,
        )

        log_ok = False
        if updated["log_channel_id"] and updated["log_message_id"]:
            try:
                lch = interaction.guild.get_channel(int(updated["log_channel_id"]))
                if lch:
                    lmsg = await lch.fetch_message(int(updated["log_message_id"]))
                    await lmsg.edit(embed=new_embed)
                    log_ok = True
            except Exception:
                pass

        dm_ok = False
        if updated["dm_channel_id"] and updated["dm_message_id"]:
            try:
                dch  = await self.bot.fetch_channel(int(updated["dm_channel_id"]))
                dmsg = await dch.fetch_message(int(updated["dm_message_id"]))
                dm_embed = _build_infraction_embed(
                    punishment     = updated["punishment"],
                    reason         = updated["reason"],
                    case_id        = case_id,
                    issuer         = issuer_m,
                    issuer_mention = issuer_mention,
                    status         = updated["status"],
                    created_at     = updated["created_at"],
                    member         = member,
                    member_id      = updated["user_id"] if not member else None,
                    guild          = interaction.guild,
                )
                await dmsg.edit(embed=dm_embed)
                dm_ok = True
            except Exception:
                pass

        await interaction.followup.send(
            embed=ok_embed(
                f"Case #{case_id} updated",
                f"**Punishment:** {updated['punishment']}\n"
                f"**Reason:** {updated['reason']}\n"
                f"**Status:** {updated['status']}\n"
                f"Log updated: {'✅' if log_ok else '❌'}\n"
                f"DM updated: {'✅' if dm_ok else '❌'}",
            ),
            ephemeral=True,
        )

    # ── /infractions mass ─────────────────────────────────────

    @infractions_group.command(
        name="mass",
        description="Issue the same infraction to multiple members at once.",
    )
    @app_commands.describe(
        punishment="Type of punishment",
        reason="Reason for the infraction",
    )
    @app_commands.choices(
        punishment=[app_commands.Choice(name=t, value=t) for t in INFRACTION_TYPES]
    )
    async def mass_infract(
        self,
        interaction: discord.Interaction,
        punishment: str,
        reason: str,
    ):
        await interaction.response.defer(ephemeral=True)
        if not self._guard(interaction.user):
            await interaction.followup.send(
                embed=err_embed("You are not authorised to issue infractions."),
                ephemeral=True,
            )
            return

        log_ch = interaction.guild.get_channel(int(INFRACTION_LOG_CHANNEL_ID))
        if not log_ch:
            await interaction.followup.send(
                embed=err_embed("Infraction log channel not configured."), ephemeral=True
            )
            return

        embed = info_embed(
            f"Mass {punishment}",
            f"**Reason:** {reason}\n\n"
            "Select members from the menu below, then click **Issue to Selected**.",
        )
        view = MassInfractView(
            punishment=punishment,
            reason=reason,
            issuer=interaction.user,
            log_ch=log_ch,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Infractions(bot))