"""
cogs/staff.py — resign, promote (with rank security), mterm, munban,
                auto-perm sync, and cross-server mirrored bans.
"""
import discord
from datetime import datetime
from discord import app_commands
from discord.ext import commands
from utils.logger import (
    log_ban, log_unban, log_kick,
    log_mterm, log_munban,
)
from utils.helpers import (
    is_hr, is_senior_hr, is_staff, is_ia,
    can_promote_to,
    err_embed, ok_embed, neutral_embed,
)
from configs.main import (
    GUILD_ID,
    OWNERSHIP_ROLE_ID, DIRECTIVE_TEAM_ROLE_ID, MANAGEMENT_TEAM_ROLE_ID,
    INTERNAL_AFFAIRS_ROLE_ID, ADMINISTRATION_ROLE_ID, MODERATION_ROLE_ID,
    FORMER_STAFF_ROLE_ID,
    ALL_STAFF_ROLE_IDS, STAFF_HIERARCHY,
    RESIGNATION_LOG_CHANNEL_ID, PROMOTION_LOG_CHANNEL_ID,
    COLOUR_BRAND,
    EMBED_IMAGE_PROMOTION, EMBED_IMAGE_RESIGNATION,
    DEPARTMENT_SERVERS,
)

STAFF_TEAM_ROLE_ID = 1530738492043431955
SUPPORT_TEAM_ROLE_ID = 1530738492047360124
LOA_ROLE_ID = 1530738492072525861
ON_DUTY_ROLE_ID = 1530738492064403531


# =============================================================
# COG
# =============================================================

def _get_actual_rank(member: discord.Member, guild: discord.Guild):
        """Highest-positioned role the member holds that outranks Staff Team,
        ignoring LOA/On-Duty status roles (those are duty-state, not rank).
        Returns None if they hold nothing above Staff Team."""
        staff_team_role = guild.get_role(STAFF_TEAM_ROLE_ID)
        if staff_team_role is None:
            return None

        excluded_ids = {LOA_ROLE_ID, ON_DUTY_ROLE_ID}
        candidates = [
            r for r in member.roles
            if r.id not in excluded_ids and r.position > staff_team_role.position
        ]
        return max(candidates, key=lambda r: r.position, default=None)

class Staff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # =========================================================
    # /resign
    # =========================================================
    @app_commands.command(name="resign",
                          description="Submit a resignation and remove all staff roles.")
    @app_commands.describe(member="The staff member resigning (defaults to yourself)")
    async def resign(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        if member and member != interaction.user:
            if not is_hr(interaction.user):
                await interaction.response.send_message(
                    embed=err_embed("Only **High Ranking** can resign other members."),
                    ephemeral=True)
                return
            target = member
        else:
            target = interaction.user

        if not is_staff(target):
            await interaction.response.send_message(
                embed=err_embed(f"{target.mention} does not have a staff role."),
                ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        actual_rank = _get_actual_rank(target, interaction.guild)

        extra_role_ids = {
            LOA_ROLE_ID, ON_DUTY_ROLE_ID, STAFF_TEAM_ROLE_ID, SUPPORT_TEAM_ROLE_ID,
        }
        roles_to_remove = [
            interaction.guild.get_role(rid)
            for rid in ALL_STAFF_ROLE_IDS
            if interaction.guild.get_role(rid) and interaction.guild.get_role(rid) in target.roles
        ]
        for rid in extra_role_ids:
            role = interaction.guild.get_role(rid)
            if role and role in target.roles and role not in roles_to_remove:
                roles_to_remove.append(role)
        if actual_rank and actual_rank not in roles_to_remove:
            roles_to_remove.append(actual_rank)

        former_role = interaction.guild.get_role(FORMER_STAFF_ROLE_ID)

        role_error = False
        try:
            if roles_to_remove:
                await target.remove_roles(*roles_to_remove, reason="Resignation")
            if former_role and former_role not in target.roles:
                await target.add_roles(former_role, reason="Resignation")
        except discord.Forbidden:
            role_error = True

        embed = discord.Embed(
            title="Staff Resignation",
            description=(
                f"We regret to inform you that {target.mention} has resigned from their "
                f"position as {actual_rank.mention if actual_rank else 'staff'}. "
                f"We appreciate the work they contributed during their time with us and "
                f"thank them for their efforts."
                f"\n\n*Issued by:*\n"
                f"{interaction.user.mention} on behalf of {interaction.guild.name}."
            ),
            color=COLOUR_BRAND,
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url="https://i.imgur.com/9KCE36G.png")
        embed.set_author(
            name=f"Processed by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_image(url=EMBED_IMAGE_RESIGNATION)

        log_ch = interaction.guild.get_channel(RESIGNATION_LOG_CHANNEL_ID)
        if log_ch:
            await log_ch.send(content=target.mention, embed=embed)

        try:
            dm = discord.Embed(
                title="Staff Resignation",
                description=(
                    f"Your resignation from **{interaction.guild.name}** has been processed.\n\n"
                    f"We appreciate your contributions and thank you for your efforts."
                ),
                color=COLOUR_BRAND,
            )
            dm.set_image(url=EMBED_IMAGE_RESIGNATION)
            await target.send(embed=dm)
        except discord.Forbidden:
            pass

        if role_error:
            await interaction.followup.send(
                embed=neutral_embed(
                    "Resignation logged.",
                    f"{target.mention}'s resignation was recorded.\n\n⚠️ Roles were not changed because the bot lacks permission to edit roles.",
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=ok_embed("Resignation processed.", f"{target.mention}'s resignation has been logged."),
            ephemeral=True)

    # =========================================================
    # /promote  (rank-secured)
    # =========================================================
    @app_commands.command(name="promote",
                          description="Log a promotion for a staff member.")
    @app_commands.describe(
        member   = "The member being promoted",
        new_role = "The role they are being promoted to",
        reason   = "Reason for the promotion (optional)",
    )
    async def promote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        new_role: discord.Role,
        reason: str,
    ):
        if not is_hr(interaction.user):
            await interaction.response.send_message(
                embed=err_embed("Only **High Ranking** can log promotions."),
                ephemeral=True)
            return

        # Security: promoter must outrank the role they're promoting to
        # Owner bypass — no hierarchy restrictions
        if interaction.user.id != 637836453983813653:
            if not can_promote_to(interaction.user, new_role):
                await interaction.response.send_message(
                    embed=err_embed(
                        f"You cannot promote someone to {new_role.mention} "
                        f"as it is equal to or higher than your own rank."
                    ),
                    ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)

        role_error = False
        try:
            await member.add_roles(new_role, reason="Promotion")
        except discord.Forbidden:
            role_error = True

        embed = discord.Embed(
            title="Staff Promotion",
            description=(
                f"You've been promoted to {new_role.mention}! Your commitment and hard work "
                f"have earned you this role, and we're excited to see you take on new challenges."
            ),
            color=COLOUR_BRAND,
            timestamp=datetime.utcnow(),
        )
        embed.set_author(
            name=f"Issued by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_thumbnail(url="https://i.imgur.com/9KCE36G.png")
        embed.add_field(name="User",   value=member.mention, inline=True)
        embed.add_field(name="New Role", value=new_role.mention,         inline=True)
        embed.add_field(name="Reason",   value=reason,                   inline=False)
        embed.set_image(url=EMBED_IMAGE_PROMOTION)

        log_ch = interaction.guild.get_channel(PROMOTION_LOG_CHANNEL_ID)
        if log_ch:
            await log_ch.send(content=member.mention, embed=embed)

        try:
            dm = discord.Embed(
                title="Staff Promotion",
                description=(
                    f"You've been promoted to {new_role.mention} in **{interaction.guild.name}**!\n"
                    f"Your commitment and hard work have earned you this role."
                ),
                color=COLOUR_BRAND,
            )
            dm.add_field(name="Reason", value=reason,                   inline=True)
            dm.add_field(name="User", value=member.mention, inline=True)
            dm.set_image(url=EMBED_IMAGE_PROMOTION)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        if role_error:
            await interaction.followup.send(
                embed=neutral_embed(
                    "Promotion logged.",
                    f"{member.mention} → {new_role.mention}\n\n⚠️ Roles were not changed because the bot lacks permission to edit roles.",
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=ok_embed("Promotion logged.", f"{member.mention} → {new_role.mention}"),
            ephemeral=True)

    # =========================================================
    # -mterm  — mass terminate across all servers (prefix only)
    # =========================================================
    @commands.command(name="mterm")
    async def mterm(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided.",
    ):
        interaction = None  # not slash
        if not is_senior_hr(ctx.author):
            await ctx.send(embed=err_embed("Only **Senior High Ranking** can use mterm."))
            return

        results = []

        # ── Main server — remove ALL roles except @everyone ─────
        RESIDENT_NAME = "resident of atlanta"
        removed = [
            r for r in member.roles
            if r != ctx.guild.default_role
            and r.name.lower() != RESIDENT_NAME
        ]
        try:
            if removed:
                await member.remove_roles(*removed, reason=f"mterm: {reason}")
            results.append(f"✅ **{ctx.guild.name}** — {len(removed)} roles removed")
        except discord.Forbidden:
            results.append(f"❌ **{ctx.guild.name}** — missing permissions")

        # ── Department servers ────────────────────────────────
        for guild_id, dcfg in DEPARTMENT_SERVERS.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                results.append(f"⚠️ **{dcfg['name']}** — bot not in server")
                continue
            dept_member = guild.get_member(member.id)
            if not dept_member:
                results.append(f"➖ **{dcfg['name']}** — user not in server")
                continue
            dept_roles = [
                r for r in dept_member.roles
                if r != guild.default_role
                and r.name.lower() != "resident of atlanta"
            ]
            try:
                if dept_roles:
                    await dept_member.remove_roles(*dept_roles, reason=f"mterm: {reason}")
                results.append(f"✅ **{dcfg['name']}** — {len(dept_roles)} roles removed")
            except discord.Forbidden:
                results.append(f"❌ **{dcfg['name']}** — missing permissions")

        embed = discord.Embed(
            title="Mass Termination",
            description=f"**Target:** {member.mention}\n**Reason:** {reason}\n\n" + "\n".join(results),
            color=COLOUR_BRAND,
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text=f"Issued by {ctx.author}")
        await ctx.send(embed=embed)
        await log_mterm(self.bot, ctx.guild, member, ctx.author, reason, results)
    # =========================================================
    # -munban — unban from all servers (prefix only)
    # =========================================================
    @commands.command(name="munban")
    async def munban(
        self,
        ctx: commands.Context,
        user_id: str,
        *,
        reason: str = "No reason provided.",
    ):
        if not is_senior_hr(ctx.author):
            await ctx.send(embed=err_embed("Only **Senior High Ranking** can use munban."))
            return

        try:
            uid = int(user_id)
        except ValueError:
            await ctx.send(embed=err_embed("Invalid user ID."))
            return

        user = await self.bot.fetch_user(uid)
        results = []

        all_guilds = [ctx.guild] + [
            self.bot.get_guild(gid)
            for gid in DEPARTMENT_SERVERS
            if self.bot.get_guild(gid)
        ]

        for guild in all_guilds:
            if not guild:
                continue
            try:
                await guild.unban(user, reason=f"munban: {reason}")
                results.append(f"✅ **{guild.name}** — unbanned")
            except discord.NotFound:
                results.append(f"➖ **{guild.name}** — not banned")
            except discord.Forbidden:
                results.append(f"❌ **{guild.name}** — missing permissions")

        embed = discord.Embed(
            title="Mass Unban",
            description=f"**User:** {user.mention} (`{uid}`)\n**Reason:** {reason}\n\n" + "\n".join(results),
            color=COLOUR_BRAND,
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text=f"Issued by {ctx.author}")
        await ctx.send(embed=embed)
        await log_munban(self.bot, ctx.guild, user, ctx.author, reason, results)

    # =========================================================
    # MIRRORED BANS, UNBANS + KICKS  (main server → departments)
    # =========================================================

    async def _get_audit_reason(self, guild, user, action, prefix):
        import asyncio
        await asyncio.sleep(1)
        try:
            async for entry in guild.audit_logs(limit=10, action=action):
                if entry.target.id == user.id:
                    return f"[{prefix}] {entry.reason or 'No reason provided.'}"
        except discord.Forbidden:
            pass
        return f"[{prefix}] Mirrored from {guild.name}"

    @commands.Cog.listener()
    async def on_guild_ban(self, guild: discord.Guild, user: discord.User):
        if guild.id != GUILD_ID:
            return
        import asyncio; await asyncio.sleep(1)
        reason = "No reason provided."
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
                if entry.target.id == user.id:
                    reason = entry.reason or "No reason provided."
                    break
        except discord.Forbidden:
            pass
        mirrored = []
        for guild_id, dcfg in DEPARTMENT_SERVERS.items():
            dept_guild = self.bot.get_guild(guild_id)
            if not dept_guild:
                continue
            try:
                await dept_guild.ban(user, reason=f"[Mirror Ban] {reason}", delete_message_days=0)
                mirrored.append(f"✅ {dcfg['name']}")
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as ex:
                mirrored.append(f"❌ {dcfg['name']} ({ex})")
        await log_ban(self.bot, guild, user, reason, mirrored)

    @commands.Cog.listener()
    async def on_guild_unban(self, guild: discord.Guild, user: discord.User):
        if guild.id != GUILD_ID:
            return
        import asyncio; await asyncio.sleep(1)
        reason = "No reason provided."
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
                if entry.target.id == user.id:
                    reason = entry.reason or "No reason provided."
                    break
        except discord.Forbidden:
            pass
        mirrored = []
        for guild_id, dcfg in DEPARTMENT_SERVERS.items():
            dept_guild = self.bot.get_guild(guild_id)
            if not dept_guild:
                continue
            try:
                await dept_guild.unban(user, reason=f"[Mirror Unban] {reason}")
                mirrored.append(f"✅ {dcfg['name']}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
                mirrored.append(f"❌ {dcfg['name']} ({ex})")
        await log_unban(self.bot, guild, user, mirrored)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.guild.id != GUILD_ID:
            return
        import asyncio
        await asyncio.sleep(1)
        try:
            async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target.id == member.id:
                    reason  = entry.reason or "No reason provided."
                    mirrored = []
                    for guild_id, dcfg in DEPARTMENT_SERVERS.items():
                        dept_guild  = self.bot.get_guild(guild_id)
                        if not dept_guild:
                            continue
                        dept_member = dept_guild.get_member(member.id)
                        if not dept_member:
                            mirrored.append(f"➖ {dcfg['name']} (not in server)")
                            continue
                        try:
                            await dept_guild.kick(dept_member,
                                                  reason=f"[Mirror Kick] {reason}")
                            mirrored.append(f"✅ {dcfg['name']}")
                        except (discord.Forbidden, discord.HTTPException) as ex:
                            mirrored.append(f"❌ {dcfg['name']} ({ex})")
                    await log_kick(self.bot, member.guild, member, reason, mirrored)
                    return
        except discord.Forbidden:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Staff(bot))
