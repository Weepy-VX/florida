import json
import re
import discord
from discord.ext import commands

class EmbedRetriever(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.link_regex = re.compile(
            r"https://(?:ptb\.|canary\.)?discord\.com/channels/(\d+)/(\d+)/(\d+)"
        )

    @commands.command(name="retrieve")
    async def retrieve_layout(self, ctx, target: str = None):
        """Retrieves messages formatted explicitly for copy-pasting into Discohook."""
        target_message = None

        # Scenario 1: User replied to a message
        if ctx.message.reference and ctx.message.reference.message_id:
            ref_msg_id = ctx.message.reference.message_id
            target_message = ctx.message.reference.cached_message or \
                             await self._safe_fetch(ctx.channel, ref_msg_id)

        # Scenario 2: Argument passed (Link or ID)
        elif target:
            link_match = self.link_regex.search(target)
            if link_match:
                _, channel_id, message_id = map(int, link_match.groups())
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                target_message = await self._safe_fetch(channel, message_id)
            elif target.isdigit():
                target_message = await self._safe_fetch(ctx.channel, int(target))

        if not target_message:
            return await ctx.send("❌ Message not found or inaccessible.")

        # Initialize standard Discohook root schema dictionary
        discohook_payload = {}
        
        # Include normal message content if it exists
        if target_message.content:
            discohook_payload["content"] = target_message.content

        # Check for Components V2 (Message Flags contain IS_COMPONENTS_V2 bit 15)
        is_v2 = bool(target_message.flags.value & (1 << 15)) or (target_message.components and not target_message.embeds)

        if is_v2:
            # V2 Layouts map directly inside a top-level components array
            discohook_payload["components"] = [comp.to_dict() for comp in target_message.components]
            payload_type = "Components V2"
        elif target_message.embeds:
            # Traditional layouts map inside a top-level embeds array
            discohook_payload["embeds"] = [embed.to_dict() for embed in target_message.embeds]
            payload_type = "Legacy Embeds"
        elif not target_message.content:
            return await ctx.send("❌ This message contains no text, embeds, or V2 layouts to parse.")

        # Convert dictionary to pretty string format
        json_string = json.dumps(discohook_payload, indent=4)

        header = f"📋 **Discohook-Ready JSON ({payload_type if 'payload_type' in locals() else 'Plain Text'}):**\n"
        
        # Keep inside blocks if it respects Discord's 2000 character limits
        if len(json_string) + len(header) <= 1985:
            await ctx.send(f"{header}```json\n{json_string}\n```")
        else:
            import io
            file_data = io.BytesIO(json_string.encode("utf-8"))
            discord_file = discord.File(fp=file_data, filename="discohook_share.json")
            await ctx.send(f"📂 Structure is too long for chat. Download this file and paste its contents into Discohook:", file=discord_file)

    async def _safe_fetch(self, channel, msg_id):
        try:
            return await channel.fetch_message(msg_id)
        except:
            return None

async def setup(bot):
    await bot.add_cog(EmbedRetriever(bot))