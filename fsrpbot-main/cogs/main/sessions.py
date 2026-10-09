"""
cogs/sessions.py — Session management (SSU/SSD) and the live status card.
"""
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from configs.main import (
    COLOUR_BRAND,
    SESSION_CHANNEL_ID,
    SESSION_CHECK_INTERVAL,
    SESSION_FULL_THRESHOLD,
    SESSION_PING_ROLE_ID,
)
from utils.db import (
    create_session_log,
    get_session,
    # Session boost logging is disabled.
    # increment_boost,
    update_peak_players,
    upsert_session,
)
from utils.helpers import (
    erlc_server_snapshot,
    err_embed,
    is_senior_hr,
    ok_embed,
)
from utils.logger import log_session_start
# Session shutdown/boost log helpers are currently unused/disabled.
# from utils.logger import log_session_boost, log_session_end, log_session_start

# Channel the static session panel lives in, and the two banner images used
# to frame it (top hero banner + bottom footer banner).
SESSION_PANEL_CHANNEL_ID = 1531158037052526684
SESSION_PANEL_TOP_IMAGE = "https://i.imgur.com/e72iEAY.png"
SESSION_PANEL_BOTTOM_IMAGE = "https://i.imgur.com/y13i2M7.png"

# The live status card is always this exact, pre-existing message — it is
# edited in place every tick and NEVER reposted/recreated, regardless of
# what's in the DB. https://discord.com/channels/1530738492018135171/1531158037052526684/1533557555098550297
SESSION_STATUS_MESSAGE_ID = 1533557555098550297
SESSION_ANNOUNCE_ACCENT_COLOR = 15437332
SESSION_ANNOUNCE_THUMBNAIL = "https://i.imgur.com/9KCE36G.png"
SESSION_STARTUP_JOIN_URL = "https://erlc.gg/join?code=FLA"
# Discord link buttons cannot carry a custom_id.
# SESSION_STARTUP_JOIN_CUSTOM_ID = "p_337038355902501022"
SESSION_SHUTDOWN_PING_CUSTOM_ID = "p_337040258795311106"


def _fmt_duration(secs):
    if secs is None:
        return "N/A"
    h, r = divmod(int(secs), 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


class SessionStatusLayout(discord.ui.LayoutView):
    """The live status card, in Components V2 (requires discord.py 2.6+).

    Replaces the old 3-embed layout (top banner / body / footer embeds),
    which rendered at inconsistent widths depending on content — a plain
    image embed and a fielded embed don't render the same width. Each of
    these three Containers renders at the same, full message width.

    The old SessionStatusView's button row (Server Status indicator /
    Session Ping toggle / Join Server link) is folded into the footer
    Container's ActionRow, since buttons can't stand alone in a
    LayoutView — they must live inside an ActionRow. custom_ids are kept
    identical to the old buttons so persistent-view dispatch (bot.add_view)
    keeps working across the migration.
    """

    def __init__(self, cog, snapshot: dict | None, online: bool, last_updated: datetime | None):
        super().__init__(timeout=None)
        self.cog = cog

        intro_text = (
            "> Welcome to Florida State RP!  Our sessions are not hosted on a schedule. "
            "They occur whenever both our community members and our server staff are available "
            "to join and initiate a server full of immersive roleplays. If you vote, you are "
            "**required** to join. Please see below for our current status:"
        )

        name = snapshot.get("name") if snapshot else "N/A"
        join_code = (snapshot.get("join_code") if snapshot else None) or "FLA"
        if snapshot and online:
            players_line = f"{snapshot.get('players_count', 0)}/{snapshot.get('max_players', 'N/A')}"
            staff_line = snapshot.get("staff_count", 0)
            queue_line = snapshot.get("queue_count", 0)
        else:
            players_line = "Offline"
            staff_line = 0
            queue_line = 0

        top_container = discord.ui.Container(
            discord.ui.MediaGallery(discord.MediaGalleryItem(SESSION_PANEL_TOP_IMAGE)),
            accent_color=COLOUR_BRAND,
        )

        body_container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(intro_text),
                accessory=discord.ui.Thumbnail("https://i.imgur.com/9KCE36G.png"),
            ),
            discord.ui.TextDisplay(f"**Server Name:** {name}\n**Server Code:** {join_code}"),
            accent_color=COLOUR_BRAND,
        )

        status_button = discord.ui.Button(
            label="Server Online" if online else "Server Offline",
            style=discord.ButtonStyle.success if online else discord.ButtonStyle.danger,
            disabled=True,
            custom_id="p_330843653800988673",
        )
        ping_button = discord.ui.Button(
            label="Session Ping",
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji(id=1533247001817387058, name="unknown", animated=False),
            custom_id="p_330843749917659137",
        )
        ping_button.callback = self._ping_toggle_callback
        join_button = discord.ui.Button(
            label="Join Server",
            style=discord.ButtonStyle.link,
            url="https://erlc.gg/join/FLA",
        )

        footer_container = discord.ui.Container(
            discord.ui.TextDisplay(
                f"**Player Count:** {players_line}\n"
                f"**Staff In-game:** {staff_line}\n"
                f"**Queue:** {queue_line}\n"
                f"Last updated: {cog._fmt_discord_timestamp(last_updated)}"
            ),
            discord.ui.MediaGallery(discord.MediaGalleryItem(SESSION_PANEL_BOTTOM_IMAGE)),
            accent_color=COLOUR_BRAND,
        )

        self.add_item(top_container)
        self.add_item(body_container)
        self.add_item(footer_container)
        self.add_item(discord.ui.ActionRow(status_button, ping_button, join_button))

    async def _ping_toggle_callback(self, interaction: discord.Interaction):
        await self.cog._toggle_session_ping(interaction)


class SessionShutdownLayout(discord.ui.LayoutView):
    """Session shutdown announcement in Components V2."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

        ping_button = discord.ui.Button(
            label="Session Ping",
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji(id=1531440112183804045, name="fla", animated=False),
            custom_id=SESSION_SHUTDOWN_PING_CUSTOM_ID,
        )
        ping_button.callback = self._session_ping_callback

        self.add_item(
            discord.ui.Container(
                discord.ui.MediaGallery(discord.MediaGalleryItem(SESSION_PANEL_TOP_IMAGE)),
                accent_color=SESSION_ANNOUNCE_ACCENT_COLOR,
            )
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        "# Session Shutdown\n"
                        "> Thank you for a great session! We look forward to seeing you in our next! "
                        "Click the button below to receive the Session Ping role so you don't miss out!"
                    ),
                    accessory=discord.ui.Thumbnail(SESSION_ANNOUNCE_THUMBNAIL),
                ),
                discord.ui.MediaGallery(discord.MediaGalleryItem(SESSION_PANEL_BOTTOM_IMAGE)),
                accent_color=SESSION_ANNOUNCE_ACCENT_COLOR,
            )
        )
        self.add_item(discord.ui.ActionRow(ping_button))

    async def _session_ping_callback(self, interaction: discord.Interaction):
        await self.cog._add_session_ping(interaction)


class SessionStartupLayout(discord.ui.LayoutView):
    """Session startup announcement in Components V2."""

    def __init__(self):
        super().__init__(timeout=None)

        join_button = discord.ui.Button(
            label="Join up!",
            style=discord.ButtonStyle.link,
            emoji=discord.PartialEmoji(id=1531440112183804045, name="unknown", animated=False),
            url=SESSION_STARTUP_JOIN_URL,
        )

        self.add_item(
            discord.ui.Container(
                discord.ui.MediaGallery(discord.MediaGalleryItem(SESSION_PANEL_TOP_IMAGE)),
                accent_color=SESSION_ANNOUNCE_ACCENT_COLOR,
            )
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        "# Session Startup\n"
                        "> Our session is starting up! Join in-game with code **FLA** and let's get some "
                        "amazing roleplays started and have some fun together!"
                    ),
                    accessory=discord.ui.Thumbnail(SESSION_ANNOUNCE_THUMBNAIL),
                ),
                discord.ui.ActionRow(join_button),
                discord.ui.MediaGallery(discord.MediaGalleryItem(SESSION_PANEL_BOTTOM_IMAGE)),
                accent_color=SESSION_ANNOUNCE_ACCENT_COLOR,
            )
        )


class Sessions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._full_msg: dict[int, discord.Message] = {}
        self._full_msg_started_at: dict[int, datetime] = {}
        # Session boosts are disabled.
        # self._full_boost_notified: set[int] = set()
        self._status_msg: dict[int, discord.Message] = {}
        self.session_monitor.start()

    async def cog_load(self):
        # Re-register the status card's persistent button row (Server
        # Status / Session Ping / Join Server) so it keeps responding
        # after a restart.
        self.bot.add_view(SessionStatusLayout(self, snapshot=None, online=False, last_updated=None))
        self.bot.add_view(SessionShutdownLayout(self))

    def cog_unload(self):
        self.session_monitor.cancel()

    # ---------------------------------------------------------------
    # /session command group — replaces the old button-based management
    # panel (SSU/SSD/Poll buttons posted via /sessionpanel, which
    # no longer exists).
    # ---------------------------------------------------------------

    @commands.hybrid_group(name="session", description="Manage the current session.")
    async def session_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=err_embed("Use `/session startup` or `/session shutdown`."),
                ephemeral=True,
            )

    @session_group.command(name="startup", description="Start a new session (SSU).")
    async def session_startup_cmd(self, ctx: commands.Context):
        if not is_senior_hr(ctx.author):
            await ctx.send(embed=err_embed("You need the **Senior High Ranking** role."), ephemeral=True)
            return
        await self._start_session(ctx, "SSU")

    @session_group.command(name="shutdown", description="End the current session (SSD).")
    async def session_shutdown_cmd(self, ctx: commands.Context):
        if not is_senior_hr(ctx.author):
            await ctx.send(embed=err_embed("You need the **Senior High Ranking** role."), ephemeral=True)
            return
        await self._end_session(ctx, "SSD")

    # Session boosts are disabled.
    # @session_group.command(name="boost", description="Announce that session slots are open.")
    # async def session_boost_cmd(self, ctx: commands.Context):
    #     if not is_senior_hr(ctx.author):
    #         await ctx.send(embed=err_embed("You need the **Senior High Ranking** role."), ephemeral=True)
    #         return
    #     await self._boost_session(ctx)

    # ---------------------------------------------------------------
    # Live session status card — the "server info" style embed (top
    # banner, live player count, last-updated footer, bottom banner)
    # inspired by other ERLC session channels. Auto-created once and
    # then always edited in place on the monitor loop — never resent.
    # ---------------------------------------------------------------

    def _build_full_embed(self, count: int, full_since: str) -> discord.Embed:
        embed = discord.Embed(
            title="Session Full",
            description=(
                f"Our session has been full since {full_since}. Thank you to everyone who joined!"
            ),
            color=COLOUR_BRAND,
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text=f"Players in-game: {count}")
        return embed

    def _fmt_relative_time(self, when: datetime | None) -> str:
        if not when:
            return "just now"
        delta = int((datetime.utcnow() - when).total_seconds())
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        hours, minutes = divmod(delta // 60, 60)
        return f"{hours}h {minutes}m ago"

    def _fmt_discord_timestamp(self, when: datetime | None) -> str:
        if not when:
            return "<t:0:R>"
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return f"<t:{int(when.timestamp())}:R>"

    async def _sync_status_card(
        self,
        guild: discord.Guild,
        count: int | None,
        online: bool,
        sess: dict | None,
        snapshot: dict | None = None,
        fetch: bool = True,
    ):
        channel = guild.get_channel(SESSION_PANEL_CHANNEL_ID)
        if not channel:
            print(
                f"[SESSION STATUS CARD] guild={guild.id} could not find panel channel "
                f"{SESSION_PANEL_CHANNEL_ID} (wrong ID, deleted, or it's a thread — "
                f"guild.get_channel() doesn't resolve threads, use guild.get_thread())"
            )
            return

        # `fetch=False` lets callers that already loaded one snapshot for
        # this tick (e.g. session_monitor looping over every guild the bot
        # is in) reuse it instead of re-hitting the ER:LC API once per
        # guild — the ER:LC server itself doesn't change per Discord guild,
        # so refetching per guild was needlessly burning through the rate
        # limit as soon as the bot was in more than one server.
        if fetch:
            snapshot = await erlc_server_snapshot() if online else None
        last_updated = datetime.utcnow()
        layout = SessionStatusLayout(self, snapshot, online, last_updated)
        gid = guild.id

        # Always edit this one specific, pre-existing message. Never
        # channel.send() a new one — that's what was causing the spam.
        # Cache the fetched Message object so most ticks don't even need
        # a fetch_message() round trip, just the edit.
        msg = self._status_msg.get(gid)
        if not msg:
            try:
                msg = await channel.fetch_message(SESSION_STATUS_MESSAGE_ID)
                self._status_msg[gid] = msg
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(
                    f"[SESSION STATUS CARD] guild={gid} could not fetch status message "
                    f"{SESSION_STATUS_MESSAGE_ID}: {exc!r}"
                )
                return

        try:
            # content=None clears any leftover text content. Embeds must be
            # cleared with an empty list, NOT None — discord.py's edit()
            # calls len() on whatever you pass to `embeds`, so passing None
            # raises "TypeError: object of type 'NoneType' has no len()".
            # This matters the first time a message migrates from old
            # embeds to Components V2, and is harmless to keep passing on
            # every subsequent edit too.
            await msg.edit(content=None, embeds=[], view=layout)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            print(f"[SESSION STATUS CARD] guild={gid} edit failed on {SESSION_STATUS_MESSAGE_ID}: {exc!r}")
            self._status_msg.pop(gid, None)

    async def _toggle_session_ping(self, interaction: discord.Interaction):
        if not SESSION_PING_ROLE_ID:
            await interaction.response.send_message(
                embed=err_embed("The session ping role isn't configured."),
                ephemeral=True,
            )
            return
        role = interaction.guild.get_role(int(SESSION_PING_ROLE_ID))
        if not role:
            await interaction.response.send_message(
                embed=err_embed("The session ping role couldn't be found."),
                ephemeral=True,
            )
            return

        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Session ping toggle")
                await interaction.response.send_message(
                    embed=ok_embed("Session ping removed", "You'll no longer be pinged for sessions."),
                    ephemeral=True,
                )
            else:
                await member.add_roles(role, reason="Session ping toggle")
                await interaction.response.send_message(
                    embed=ok_embed("Session ping added", "You'll now be pinged for sessions."),
                    ephemeral=True,
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=err_embed("I don't have permission to manage that role."),
                ephemeral=True,
            )

    async def _add_session_ping(self, interaction: discord.Interaction):
        if not SESSION_PING_ROLE_ID:
            await interaction.response.send_message(
                embed=err_embed("The session ping role isn't configured."),
                ephemeral=True,
            )
            return
        role = interaction.guild.get_role(int(SESSION_PING_ROLE_ID))
        if not role:
            await interaction.response.send_message(
                embed=err_embed("The session ping role couldn't be found."),
                ephemeral=True,
            )
            return

        member = interaction.user
        if role in member.roles:
            await interaction.response.send_message(
                embed=ok_embed("Session ping already added", "You'll be pinged for future sessions."),
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(role, reason="Session shutdown ping button")
            await interaction.response.send_message(
                embed=ok_embed("Session ping added", "You'll be pinged for future sessions."),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=err_embed("I don't have permission to manage that role."),
                ephemeral=True,
            )

    async def _refresh_status_card(self, guild: discord.Guild):
        """Immediate refresh, used right after SSU/SSD so the card
        doesn't wait for the next monitor tick."""
        snapshot = await erlc_server_snapshot()
        count = snapshot.get("players_count") if snapshot else None
        # ERLC automatically shuts a private server's game instance down
        # once it's empty, so 0 players effectively means offline even
        # though the API itself may still answer with valid (zeroed) data.
        online = snapshot is not None and (count or 0) > 0
        await self._sync_status_card(guild, count, online, get_session(guild.id), snapshot=snapshot, fetch=False)

    async def _start_session(self, ctx: commands.Context, kind: str):
        now = datetime.utcnow()
        now_iso = now.isoformat()
        session_log_id = create_session_log(ctx.guild.id, kind, ctx.author.id, now_iso)
        upsert_session(
            ctx.guild.id,
            active=1,
            type=kind,
            started_at=now_iso,
            host_id=ctx.author.id
        )
        log_ch = ctx.guild.get_channel(int(SESSION_CHANNEL_ID))
        # Replaced by the Components V2 startup layout.
        # embed = discord.Embed(
        #     title=f"{kind} — Session Started",
        #     color=COLOUR_BRAND,
        #     timestamp=now,
        # )
        # embed.add_field(name="Host", value=ctx.author.mention, inline=True)
        if log_ch:
            await log_ch.send(view=SessionStartupLayout())
        await ctx.send(
            embed=ok_embed(f"{kind} started!"),
            ephemeral=True,
        )
        await log_session_start(self.bot, ctx.guild, ctx.author, kind, session_log_id)
        await self._refresh_status_card(ctx.guild)

    async def _end_session(self, ctx: commands.Context, kind: str):
        sess = get_session(ctx.guild.id)
        now = datetime.utcnow()

        full_msg = self._full_msg.pop(ctx.guild.id, None)
        self._full_msg_started_at.pop(ctx.guild.id, None)
        # Session boosts are disabled.
        # self._full_boost_notified.discard(ctx.guild.id)
        if full_msg:
            try:
                await full_msg.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass

        duration_str = "N/A"
        if sess and sess.get("started_at"):
            try:
                start = datetime.fromisoformat(sess["started_at"])
                duration_str = _fmt_duration((now - start).total_seconds())
            except Exception:
                pass

        upsert_session(ctx.guild.id, active=0)

        log_ch = ctx.guild.get_channel(int(SESSION_CHANNEL_ID))
        # Replaced by the Components V2 shutdown layout.
        # embed = discord.Embed(
        #     title=f"{kind} — Session Ended",
        #     color=COLOUR_BRAND,
        #     timestamp=now,
        # )
        # embed.add_field(name="Ended by", value=ctx.author.mention, inline=True)
        # embed.add_field(name="Duration", value=f"`{duration_str}`", inline=True)
        if log_ch:
            await log_ch.send(view=SessionShutdownLayout(self))

        await ctx.send(
            embed=ok_embed(f"{kind} complete!", f"Duration: `{duration_str}`"),
            ephemeral=True,
        )
        await self._refresh_status_card(ctx.guild)

    # Session boosts are disabled.
    # async def _boost_session(self, ctx: commands.Context):
    #     log_ch = ctx.guild.get_channel(int(SESSION_CHANNEL_ID))
    #     embed = discord.Embed(
    #         title="Session Boost",
    #         description="Join up and let's get some more roleplays going!",
    #         color=COLOUR_BRAND,
    #         timestamp=datetime.utcnow(),
    #     )
    #     if log_ch:
    #         await log_ch.send(embed=embed)
    #
    #     await ctx.send(embed=ok_embed("Session Boost sent!"), ephemeral=True)
    #     await self._refresh_status_card(ctx.guild)

    @tasks.loop(seconds=SESSION_CHECK_INTERVAL)
    async def session_monitor(self):
        await self.bot.wait_until_ready()
        # One API call per tick, shared across every guild the bot is in —
        # the ER:LC server's state doesn't depend on which Discord guild is
        # displaying it, so there's no reason to fetch it more than once.
        try:
            snapshot = await erlc_server_snapshot()
        except Exception as exc:
            # Belt-and-suspenders: erlc_server_snapshot() already catches
            # its own network errors internally and returns None, but if
            # anything unexpected still slips through, it must not be
            # allowed to kill this loop the way an earlier bug did.
            print(f"[SESSION MONITOR ERROR] erlc_server_snapshot() raised: {exc!r}")
            snapshot = None

        count = snapshot.get("players_count") if snapshot else None
        # ERLC automatically shuts a private server's game instance down
        # once it's empty, so 0 players effectively means offline even
        # though the API itself may still answer with valid (zeroed) data.
        online = snapshot is not None and (count or 0) > 0

        # Heartbeat — confirms in the logs, every tick, that the loop is
        # actually alive and what it currently believes the server state
        # is. If this line stops appearing in your console/log output,
        # the loop itself has died (see the error handler below); if it
        # keeps appearing but the card doesn't change, the problem is in
        # posting/editing the message instead, and the per-guild error
        # below will show why.
        print(f"[SESSION MONITOR] tick: online={online} players={count} "
              f"staff={snapshot.get('staff_count') if snapshot else None}")

        for guild in self.bot.guilds:
            try:
                await self._session_monitor_guild_tick(guild, snapshot, online, count)
            except Exception as exc:
                # A single guild failing (missing channel perms, a deleted
                # channel, a transient Discord error, etc.) must never take
                # the whole loop down — @tasks.loop stops permanently on
                # any unhandled exception, which is why refreshes could
                # silently stop happening at all.
                print(f"[SESSION MONITOR ERROR] guild={guild.id}: {exc!r}")

    @session_monitor.error
    async def _session_monitor_error(self, error: Exception):
        # Last resort: if something still escapes every try/except above
        # and discord.py stops the loop, don't leave it dead forever —
        # restart it after a short delay instead of silently going stale
        # until the next bot restart.
        print(f"[SESSION MONITOR FATAL] loop crashed: {error!r} — restarting in 5s")
        await asyncio.sleep(5)
        if not self.session_monitor.is_running():
            self.session_monitor.restart()

    async def _session_monitor_guild_tick(
        self, guild: discord.Guild, snapshot: dict | None, online: bool, count: int | None
    ):
        gid = guild.id
        sess = get_session(gid)
        if online and sess and sess.get("active") and sess.get("session_log_id"):
            update_peak_players(sess["session_log_id"], count)

        await self._sync_status_card(guild, count, online, sess, snapshot=snapshot, fetch=False)

        if not online:
            return  # can't read live player counts — skip full-session logic

        ch = guild.get_channel(SESSION_CHANNEL_ID)
        if not ch:
            return

        existing_msg = self._full_msg.get(gid)
        now = datetime.utcnow()

        if count >= SESSION_FULL_THRESHOLD:
            if existing_msg:
                started_at = self._full_msg_started_at.get(gid)
                elapsed = (
                    _fmt_duration((now - started_at).total_seconds())
                    if started_at
                    else "0m 0s"
                )
                try:
                    await existing_msg.edit(embed=self._build_full_embed(count, elapsed))
                except discord.NotFound:
                    self._full_msg.pop(gid, None)
                    self._full_msg_started_at.pop(gid, None)
                    existing_msg = None
                except discord.Forbidden:
                    pass

            if not existing_msg:
                self._full_msg_started_at[gid] = now
                msg = await ch.send(embed=self._build_full_embed(count, "0m 0s"))
                self._full_msg[gid] = msg
                upsert_session(gid, full_msg_id=msg.id, full_channel_id=ch.id)

            # Session boosts are disabled.
            # self._full_boost_notified.discard(gid)

        # Session boosts are disabled.
        # elif existing_msg and gid not in self._full_boost_notified:
        #     self._full_boost_notified.add(gid)
        #     if sess and sess.get("session_log_id"):
        #         increment_boost(sess["session_log_id"])
        #
        #     boost_embed = discord.Embed(
        #         title="Session Boost",
        #         description=(
        #             f"Player count has dropped to **{count}**.\n"
        #             "Join up and let's get some more roleplays going!"
        #         ),
        #         color=COLOUR_BRAND,
        #         timestamp=datetime.utcnow(),
        #     )
        #     await ch.send(embed=boost_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Sessions(bot))
