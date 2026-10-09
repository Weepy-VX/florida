"""
cogs/general.py — say, rid, ping/status commands.
"""
import asyncio
import re
import signal
import aiohttp
import discord
from discord import app_commands, components
from discord.ext import commands
from utils.helpers import (
    is_senior_hr, is_staff,
    ok_embed, err_embed, info_embed, uptime_str,
)
from configs.main import (
    COLOUR_BRAND, MELONLY_API_KEY,
)

TARGET_CHANNEL_ID = 1530738492982956177
PARTNER_REP_ROLE_ID = 1530738492030587033

# AFK system
# Structure: { user_id: {"message": str, "pings": [(author_name, jump_url)]} }
afk_db: dict[int, dict] = {}


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _format_reps(self, users: list[discord.User | discord.Member]) -> str:
        """Formats a list of user objects into the Representatives footer."""
        rep_list = "\n".join([f"- {user.mention}" for user in users])
        return f"\n\nRepresentatives:\n{rep_list}"

    async def _fetch_target_message(self, ctx_or_interaction, message_input: str) -> discord.Message | None:
        """Extracts and fetches a message object from a URL or raw ID string."""
        link_match = re.search(r"discord\.com/channels/\d+/(\d+)/(\d+)", message_input)
        
        try:
            if link_match:
                channel_id, msg_id = map(int, link_match.groups())
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                return await channel.fetch_message(msg_id)
            elif message_input.strip().isdigit():
                return await ctx_or_interaction.channel.fetch_message(int(message_input.strip()))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
            
        return None

    # ========================================================= #
    #                        PARTNERSHIP                        #
    # ========================================================= #
    @commands.hybrid_command(
        name="partnership",
        description="Forward a partnership message to the target channel with listed representatives."
    )
    @app_commands.describe(
        reps="Mention representatives (e.g. @user1 @user2)",
        message_input="Message ID or link (required if not replying to a message)"
    )
    async def partnership(
        self, 
        ctx: commands.Context, 
        reps: commands.Greedy[discord.User] = None, 
        *, 
        message_input: str = None
    ):
        target_msg = None

        # Check if executed as a reply to a message
        if ctx.message and ctx.message.reference and ctx.message.reference.message_id:
            try:
                target_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target_msg = None

        # If not a reply, try fetching via input ID or link
        if not target_msg and message_input:
            target_msg = await self._fetch_target_message(ctx, message_input)

        if not target_msg:
            await ctx.send(
                embed=err_embed("Could not find the target message. Reply to a message or pass a valid message ID/URL."),
                ephemeral=True
            )
            return

        # Parse representatives from Greedy args or raw string fallback
        final_reps = list(reps) if reps else []

        if not final_reps and message_input:
            user_ids = re.findall(r"\d+", message_input)
            for uid in user_ids:
                try:
                    user = await self.bot.fetch_user(int(uid))
                    if user and user not in final_reps:
                        final_reps.append(user)
                except discord.NotFound:
                    continue

        if not final_reps:
            await ctx.send(embed=err_embed("Please mention at least one valid representative."), ephemeral=True)
            return

        # --- NEW: Add Role to All Representatives ---
        if ctx.guild:
            role = ctx.guild.get_role(int(PARTNER_REP_ROLE_ID))
            if role:
                for user in final_reps:
                    try:
                        # Fetch user as a Member of this specific server
                        member = ctx.guild.get_member(user.id) or await ctx.guild.fetch_member(user.id)
                        if member and role not in member.roles:
                            await member.add_roles(role, reason="Partnership representative role assignment")
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        # Ignore errors if a user isn't in the server or bot lacks permissions
                        continue

        # Fetch destination channel
        dest_channel = self.bot.get_channel(TARGET_CHANNEL_ID) or await self.bot.fetch_channel(TARGET_CHANNEL_ID)
        if not dest_channel:
            await ctx.send(embed=err_embed("Target channel could not be found."), ephemeral=True)
            return

        # Prepare payload
        content = f"{target_msg.content or ''}{self._format_reps(final_reps)}".strip()
        embeds = target_msg.embeds
        files = [await attachment.to_file() for attachment in target_msg.attachments]

        # Dispatch message
        await dest_channel.send(content=content, embeds=embeds, files=files)

        # Confirm response based on context type
        if ctx.interaction:
            await ctx.send(embed=ok_embed("Partnership Posted", "Message forwarded to the target channel and roles updated."), ephemeral=True)
        else:
            try:
                await ctx.message.add_reaction("✅")
            except discord.Forbidden:
                await ctx.send(embed=ok_embed("Partnership Posted", "Message forwarded to the target channel and roles updated."))


    # =========================================================
    # SAY
    # =========================================================
    @commands.hybrid_command(name="say", description="Make the bot say something.")
    @app_commands.describe(message="The message to send")
    async def say(self, ctx: commands.Context, *, message: str):
        if not is_senior_hr(ctx.author):
            await ctx.send(embed=err_embed("You need the **Senior High Ranking** role."),
                           ephemeral=True)
            return
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.Forbidden:
                pass
            await ctx.channel.send(message)
        else:
            await ctx.interaction.response.send_message("✅ Sent.", ephemeral=True)
            await ctx.channel.send(message)

    # =========================================================
    # RID
    # =========================================================

    HEADERS = {"Authorization": f"Bearer {MELONLY_API_KEY}"}
    
    async def fetch_melonly_member(discord_id: int):
        """Helper function to fetch member details from Melonly API."""
        url = f"https://melonly.xyz{discord_id}"
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=HEADERS) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        print("Melonly API Error: Rate limit hit!")
                        return "RATE_LIMIT"
                    else:
                        print(f"Melonly API Error: Status {response.status}")
                        return None
            except Exception as e:
                print(f"Network error trying to connect to Melonly: {e}")
                return None
    
    @commands.command(name='rid', description="Get the Roblox ID of a Discord user.")
    async def get_roblox_id(self, ctx: commands.Context, target: discord.User = None):
        target_user = target or ctx.author
        discord_id = str(target_user.id)

        url = f"https://api.melonly.xyz/api/v1/verification/discord/{discord_id}/roblox"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {MELONLY_API_KEY}"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 404:
                    if target_user.id == ctx.author.id:
                        await ctx.reply("No Roblox account linked to your Discord account.")
                    else:
                        await ctx.reply(f"No Roblox account linked to {target_user.mention}.")
                    return
                elif response.status != 200:
                    await ctx.reply(f"API error occurred: `{response.status}`")
                    return
                elif response.status == 401:
                    error_text = await response.text()
                    print(f"401 Error Detail: {error_text}")
                    await ctx.reply("Authentication failed. Please check the bot log.")
                    return

                data = await response.json()
                roblox_id = data.get("robloxId")
                
                await ctx.reply(f"`{roblox_id}`")

    @get_roblox_id.error
    async def get_roblox_id_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.UserNotFound):
            await ctx.reply(f"Could not find a Discord user matching `{error.argument}`.")

    # =========================================================
    # ID — get Discord ID of a user
    # =========================================================
    @commands.command(name="id", aliases=["discordid"])
    async def get_id(self, ctx: commands.Context, member: discord.Member = None):
        target = member or ctx.author
        await ctx.send(target.id)

    # =========================================================
    # UPDATE NICKNAME
    # =========================================================
    @commands.command(name="update", description="Update a user's nickname to their Roblox username.")
    async def update_nickname(self, ctx: commands.Context, target: discord.Member = None):
        if not ctx.guild:
            await ctx.send(embed=err_embed("This command can only be used in a server."))
            return

        target_member = target or ctx.author
        discord_id = str(target_member.id)

        if not ctx.guild.me.guild_permissions.manage_nicknames:
            await ctx.send(embed=err_embed("I need the **Manage Nicknames** permission to update nicknames."))
            return

        if target_member.id == ctx.guild.owner_id:
            await ctx.send(embed=err_embed("I cannot change the server owner's nickname."))
            return

        if target_member.top_role >= ctx.guild.me.top_role:
            await ctx.send(embed=err_embed(f"I cannot edit {target_member.mention}'s nickname due to role hierarchy."))
            return

        melonly_url = f"https://api.melonly.xyz/api/v1/verification/discord/{discord_id}/roblox"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {MELONLY_API_KEY.strip().strip('\'').strip('\"')}"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(melonly_url, headers=headers) as resp:
                if resp.status == 404:
                    if target_member.id == ctx.author.id:
                        await ctx.send(embed=err_embed("No Roblox account linked to your Discord account."))
                    else:
                        await ctx.send(embed=err_embed(f"No Roblox account linked to {target_member.mention}."))
                    return
                elif resp.status != 200:
                    await ctx.send(embed=err_embed(f"Melonly API Error: `{resp.status}`"))
                    return

                data = await resp.json()
                roblox_id = data.get("robloxId")

            if not roblox_id:
                await ctx.send(embed=err_embed("Could not retrieve a valid Roblox ID."))
                return

            roblox_url = f"https://users.roblox.com/v1/users/{roblox_id}"
            async with session.get(roblox_url) as resp:
                if resp.status != 200:
                    await ctx.send(embed=err_embed(f"Failed to fetch username from Roblox API (`HTTP {resp.status}`)."))
                    return
                
                user_data = await resp.json()
                roblox_username = user_data.get("name")

        if not roblox_username:
            await ctx.send(embed=err_embed("Roblox user not found."))
            return

        try:
            old_nick = target_member.display_name
            await target_member.edit(nick=roblox_username, reason=f"Updated via -update by {ctx.author}")
            await ctx.send(embed=ok_embed(
                "Nickname Updated", 
                f"Updated {target_member.mention}'s nickname to **`{roblox_username}`**"
            ))
        except discord.Forbidden:
            await ctx.send(embed=err_embed("I don't have permission to change that member's nickname."))
        except discord.HTTPException as e:
            await ctx.send(embed=err_embed(f"Failed to update nickname: `{e}`"))

    @update_nickname.error
    async def update_nickname_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MemberNotFound):
            await ctx.send(embed=err_embed(f"Could not find a server member matching `{error.argument}`."))

    # =========================================================
    # PING / STATUS
    # =========================================================
    @commands.hybrid_command(name="ping", aliases=["status"],
                             description="Check bot latency and uptime.")
    async def ping(self, ctx: commands.Context):
        latency_ms = round(self.bot.latency * 1000)
        embed = ok_embed(
            "🏓 Pong!",
            f"**Latency:** `{latency_ms} ms`\n"
            f"**Uptime:** `{uptime_str()}`\n"
            f"**Guilds:** `{len(self.bot.guilds)}`"
        )
        await ctx.send(embed=embed)

    @commands.command(name="commands", aliases=["cmds"])
    async def list_commands(self, ctx: commands.Context):
        prefix_lines = []
        for cmd in sorted(self.bot.commands, key=lambda c: c.name):
            if cmd.hidden:
                continue
            desc = (cmd.help or cmd.brief or "No description provided.").strip()
            prefix_lines.append(f"`-{cmd.name}` — {desc}")

        slash_lines = []
        for app_cmd in sorted(self.bot.tree.get_commands(), key=lambda c: c.name):
            desc = (app_cmd.description or "No description provided.").strip()
            if desc == "…":
                desc = "No description provided."
            slash_lines.append(f"`/{app_cmd.name}` — {desc}")

        if not prefix_lines and not slash_lines:
            await ctx.send(embed=err_embed("No commands are currently registered."))
            return

        chunk_size = 15
        pages: list[discord.Embed] = []
        max_len = max(
            (len(prefix_lines) + chunk_size - 1) // chunk_size,
            (len(slash_lines) + chunk_size - 1) // chunk_size,
            1,
        )

        for i in range(max_len):
            p_start = i * chunk_size
            p_end = p_start + chunk_size
            page = ok_embed("Bot Commands")
            page.add_field(
                name="Prefix Commands",
                value="\n".join(prefix_lines[p_start:p_end]) or "*None*",
                inline=False,
            )
            page.add_field(
                name="Slash Commands",
                value="\n".join(slash_lines[p_start:p_end]) or "*None*",
                inline=False,
            )
            page.set_footer(text=f"Page {i + 1}/{max_len}")
            pages.append(page)

        for page in pages:
            await ctx.send(embed=page)

    @commands.command(name="steal")
    async def steal_emoji(self, ctx: commands.Context, emoji: str, *, name: str = ""):
        if not is_senior_hr(ctx.author):
            await ctx.send(embed=err_embed("You need the **Senior High Ranking** role."))
            return
        if not ctx.guild:
            await ctx.send(embed=err_embed("This command can only be used in a server."))
            return

        me = ctx.guild.me
        if not me or not me.guild_permissions.manage_emojis_and_stickers:
            await ctx.send(embed=err_embed("I need **Manage Expressions** permission to add emojis."))
            return

        source_url = ""
        source_name = ""

        parsed = discord.PartialEmoji.from_str(emoji)
        if parsed and parsed.id:
            source_url = str(parsed.url)
            source_name = parsed.name or "stolen_emoji"
        elif emoji.startswith("http://") or emoji.startswith("https://"):
            source_url = emoji.strip()
            source_name = "stolen_emoji"
        else:
            await ctx.send(embed=err_embed("Provide a custom emoji (e.g. `<:name:id>`) or direct image URL."))
            return

        emoji_name = (name.strip() or source_name).lower()
        emoji_name = re.sub(r"[^a-z0-9_]", "_", emoji_name)
        emoji_name = re.sub(r"_+", "_", emoji_name).strip("_")
        if not emoji_name:
            emoji_name = "stolen_emoji"
        emoji_name = emoji_name[:32]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source_url) as resp:
                    if resp.status != 200:
                        await ctx.send(embed=err_embed(f"Failed to fetch emoji image (`HTTP {resp.status}`)."))
                        return
                    image_bytes = await resp.read()
        except aiohttp.ClientError:
            await ctx.send(embed=err_embed("Could not download emoji image from the source URL."))
            return

        if len(image_bytes) > 256 * 1024:
            await ctx.send(embed=err_embed("Emoji file is too large. Discord emojis must be under 256KB."))
            return

        try:
            created = await ctx.guild.create_custom_emoji(
                name=emoji_name,
                image=image_bytes,
                reason=f"Emoji stolen by {ctx.author} ({ctx.author.id})",
            )
        except discord.Forbidden:
            await ctx.send(embed=err_embed("I don't have permission to create emojis in this server."))
            return
        except discord.HTTPException as e:
            await ctx.send(embed=err_embed(f"Could not create emoji: `{e}`"))
            return

        await ctx.send(embed=ok_embed("Emoji stolen", f"Added {created} as `:{created.name}:`"))

    # =========================================================
    # AFK SYSTEM
    # =========================================================
    @commands.command(name="afk", help="Set your AFK status.")
    async def set_afk(self, ctx: commands.Context, *, message: str = "AFK"):
        user_id = ctx.author.id

        # Store their status and initialise an empty list for missed pings
        afk_db[user_id] = {
            "message": message,
            "pings": [],
        }

        await ctx.send(embed=ok_embed(
            "AFK status set",
            f"💤 {ctx.author.mention}, I have set your AFK status to: **{message}**",
        ))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages to prevent infinite loops
        if message.author.bot:
            return

        author_id = message.author.id

        # If this message is what invoked the `afk` command, don't run the
        # "returning from AFK" check against it — otherwise we'd immediately
        # pop the status set_afk() just added a moment earlier.
        ctx = await self.bot.get_context(message)
        if ctx.valid and ctx.command and ctx.command.qualified_name == "afk":
            return

        # 1. CHECK IF THE AUTHOR IS RETURNING FROM AFK
        if author_id in afk_db:
            data = afk_db.pop(author_id)  # Removes them from AFK and grabs their data
            missed_pings = data["pings"]

            # Welcome them back in the channel
            await message.channel.send(
                f"👋 Welcome back, {message.author.mention}! I've removed your AFK status.", delete_after=5.0
            )

            # If they missed pings, DM them
            if missed_pings:
                # Format the missed pings into a readable list
                ping_text = ""
                for idx, (pinger_name, jump_url) in enumerate(missed_pings, start=1):
                    ping_text += f"{idx}. **{pinger_name}** pinged you: [Jump to message]({jump_url})\n"

                embed = discord.Embed(
                    title="You missed some pings while you were away!",
                    description=ping_text,
                    color=COLOUR_BRAND,
                )

                try:
                    await message.author.send(embed=embed)
                except discord.Forbidden:
                    # Fallback if their DMs are closed
                    await message.channel.send(
                        f"⚠️ {message.author.mention}, you missed {len(missed_pings)} pings, "
                        "but your DMs are closed!"
                    )

        # 2. CHECK IF AN AFK USER WAS PINGED
        # message.mentions catches both @mentions and direct replies
        for mentioned_user in message.mentions:
            if mentioned_user.id in afk_db and mentioned_user.id != author_id:
                afk_data = afk_db[mentioned_user.id]
                afk_message = afk_data["message"]

                # Notify the channel that the user is AFK
                await message.channel.send(
                    f"💤 **{mentioned_user.display_name}** is currently AFK: {afk_message}",
                    delete_after=10,  # Deletes after 10 seconds to reduce chat clutter
                )

                # Save the ping data (who pinged them, and the link to the message)
                afk_data["pings"].append((message.author.display_name, message.jump_url))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != 1530738492018135171:
            return
        else:
            channel = self.bot.get_channel(1530738492802465959)

            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                style=discord.ButtonStyle.grey,
                label=f"{member.guild.member_count} Members",
                emoji=discord.PartialEmoji(id=1531888445075619873, name="community"),
                custom_id="member_count",
                disabled=True
            ))
            view.add_item(discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Information",
                emoji=discord.PartialEmoji(id=1531440112183804045, name="fla"),
                url="https://discord.com/channels/1530738492018135171/1530738492446081092"
            ))

            await channel.send(
                content=f"Hey {member.mention}! Welcome to Florida State Roleplay; we hope you enjoy your stay!",
                view=view
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))