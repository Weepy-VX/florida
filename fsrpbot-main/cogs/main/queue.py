from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

QUEUE_CATEGORY_ID = 1533608689901109299

MONTH_ABBR = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sept", "oct", "nov", "dec",
]


class QueueCog(commands.Cog):
    """Handles queue/priority channel management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _date_suffix(self) -> str:
        now = datetime.now()
        return f"{MONTH_ABBR[now.month - 1]}-{now.day}"

    def _first_normal_channel(
        self, category: discord.CategoryChannel, exclude: discord.abc.GuildChannel
    ) -> discord.abc.GuildChannel | None:
        """Returns the first queue-* channel in the category (used as the
        insertion point for priority channels), or None if there isn't one."""
        for ch in category.channels:
            if ch.id == exclude.id:
                continue
            if ch.name.startswith("queue-"):
                return ch
        return None

    @app_commands.command(name="queue-add", description="Add this channel to the queue.")
    @app_commands.describe(type="Which queue to add this channel to (defaults to normal).")
    @app_commands.choices(
        type=[
            app_commands.Choice(name="normal", value="normal"),
            app_commands.Choice(name="priority", value="priority"),
            app_commands.Choice(name="instant", value="instant"),
        ]
    )
    async def queue_add(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        choice = type.value if type else "normal"
        guild = interaction.guild
        channel = interaction.channel

        if guild is None or not isinstance(channel, discord.abc.GuildChannel):
            await interaction.followup.send("This command must be used in a server text channel.", ephemeral=True)
            return

        queue_category = guild.get_channel(QUEUE_CATEGORY_ID)

        if not isinstance(queue_category, discord.CategoryChannel):
            await interaction.followup.send(
                "Could not find the configured queue category. Check the category ID.",
                ephemeral=True,
            )
            return

        try:
            if choice == "normal":
                new_name = f"queue-{self._date_suffix()}"
                await channel.edit(name=new_name)
                await channel.move(category=queue_category, end=True)

            elif choice == "priority":
                new_name = f"prty-{self._date_suffix()}"
                await channel.edit(name=new_name)
                anchor = self._first_normal_channel(queue_category, channel)
                if anchor is not None:
                    await channel.move(category=queue_category, before=anchor)
                else:
                    await channel.move(category=queue_category, end=True)

            elif choice == "instant":
                username = interaction.user.name
                new_name = f"instant-{username}"
                await channel.edit(name=new_name)
                await channel.move(category=queue_category, beginning=True)

            else:
                await interaction.followup.send("Unknown selection.", ephemeral=True)
                return

        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to rename or move this channel.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to update the channel: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"Channel updated to **{new_name}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(QueueCog(bot))