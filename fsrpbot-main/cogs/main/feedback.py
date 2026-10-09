"""
cogs/main/feedback.py — /feedback command, main server only.
"""
import discord
from datetime import datetime
from discord import app_commands
from discord.ext import commands
from utils.helpers import err_embed, ok_embed
from utils.logger import log_feedback
from configs.main import (
    COLOUR_BRAND, EMBED_IMAGE_FEEDBACK,
    FEEDBACK_LOG_CHANNEL_ID, GUILD_ID,
)

STAR_DISPLAY = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐", 4: "⭐⭐⭐⭐", 5: "⭐⭐⭐⭐⭐"}


class Feedback(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="feedback",
                          description="Submit a star rating and feedback for a staff member.")
    @app_commands.describe(
        member="The staff member you're rating",
        rating="Rating from 1 to 5 stars",
        reason="Your feedback / reason for this rating",
    )
    @app_commands.choices(rating=[
        app_commands.Choice(name="⭐ 1 — Poor",           value=1),
        app_commands.Choice(name="⭐⭐ 2 — Fair",          value=2),
        app_commands.Choice(name="⭐⭐⭐ 3 — Good",        value=3),
        app_commands.Choice(name="⭐⭐⭐⭐ 4 — Great",      value=4),
        app_commands.Choice(name="⭐⭐⭐⭐⭐ 5 — Excellent", value=5),
    ])
    async def feedback(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rating: int,
        reason: str,
    ):
        # Main server only
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                embed=err_embed("This command can only be used in the main server."),
                ephemeral=True)
            return

        if member == interaction.user:
            await interaction.response.send_message(
                embed=err_embed("You cannot submit feedback for yourself."),
                ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        stars = STAR_DISPLAY.get(rating, str(rating))
        embed = discord.Embed(
            title="Staff Feedback",
            description=f"**{interaction.user.mention}** submitted feedback for {member.mention}.",
            color=COLOUR_BRAND,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Rating",       value=stars,                    inline=True)
        embed.add_field(name="Submitted by", value=interaction.user.mention, inline=True)
        embed.add_field(name="Feedback",     value=reason,                   inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_image(url=EMBED_IMAGE_FEEDBACK)

        log_ch = interaction.guild.get_channel(FEEDBACK_LOG_CHANNEL_ID)
        if not log_ch:
            try:
                log_ch = await self.bot.fetch_channel(FEEDBACK_LOG_CHANNEL_ID)
            except Exception:
                log_ch = None
        if log_ch:
            await log_ch.send(content=member.mention, embed=embed)
        else:
            print(f"[Feedback] Could not find log channel {FEEDBACK_LOG_CHANNEL_ID}")

        try:
            dm = discord.Embed(
                title="You Received Feedback",
                description=f"You received feedback in **{interaction.guild.name}**.",
                color=COLOUR_BRAND,
            )
            dm.add_field(name="Rating",       value=stars,                    inline=True)
            dm.add_field(name="Submitted by", value=interaction.user.mention, inline=True)
            dm.add_field(name="Feedback",     value=reason,                   inline=False)
            dm.set_image(url=EMBED_IMAGE_FEEDBACK)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        await interaction.followup.send(
            embed=ok_embed("Feedback submitted!", f"Feedback for {member.mention} has been logged."),
            ephemeral=True)
        await log_feedback(self.bot, interaction.guild, interaction.user, member, rating, reason)


async def setup(bot: commands.Bot):
    await bot.add_cog(Feedback(bot))
