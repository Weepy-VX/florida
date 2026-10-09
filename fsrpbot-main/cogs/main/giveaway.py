"""
cogs/main/giveaway.py — Durable button-based giveaways for the main bot.
"""
from __future__ import annotations

import asyncio
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from configs.main import COLOUR_INFO
from utils.db import DB_PATH, init_db
from utils.helpers import err_embed, ok_embed


MIN_SEC = 60
MAX_SEC = 14 * 24 * 3600


def _parse_duration(text: str) -> int | None:
    text = (text or "").strip().lower()
    if not text:
        return None
    total = 0
    matches = list(re.finditer(r"(\d+)\s*([smhd])", text))
    if not matches:
        return None
    for m in matches:
        value = int(m.group(1))
        unit = m.group(2)
        if unit == "s":
            total += value
        elif unit == "m":
            total += value * 60
        elif unit == "h":
            total += value * 60 * 60
        elif unit == "d":
            total += value * 24 * 60 * 60
    return total if total > 0 else None


class GiveawayFromMessageModal(discord.ui.Modal, title="Start giveaway from message"):
    duration = discord.ui.TextInput(
        label="Duration",
        placeholder="e.g. 1h, 30m, 2d",
        required=True,
        max_length=24,
    )
    prize = discord.ui.TextInput(
        label="Prize",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )
    channel_id = discord.ui.TextInput(
        label="Channel ID (optional)",
        required=False,
        max_length=22,
        placeholder="Blank = post in this channel",
    )
    winners = discord.ui.TextInput(
        label="Number of winners",
        required=False,
        max_length=2,
        placeholder="1",
    )
    emoji = discord.ui.TextInput(
        label="Entry button emoji",
        required=False,
        max_length=100,
        placeholder="🎉",
    )

    def __init__(self, cog: "GiveawayCog", source_message: discord.Message):
        super().__init__(timeout=300)
        self._cog = cog
        self._source = source_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog._giveaway_from_message_submit(interaction, self)


class GiveawayCog(commands.Cog, name="Giveaways"):
    giveaway_group = app_commands.Group(name="giveaway", description="Create and manage giveaways")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(GiveawayEntryView(self.bot))
        self._ensure_tables()
        self._timers: dict[int, asyncio.Task] = {}
        self._check_expired_giveaways.start()

    def cog_unload(self) -> None:
        self._check_expired_giveaways.cancel()
        for task in self._timers.values():
            if not task.done():
                task.cancel()
        self._timers.clear()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        try:
            init_db()
        except Exception:
            pass
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaways (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    ends_at TEXT NOT NULL,
                    prize TEXT NOT NULL,
                    host_id INTEGER NOT NULL,
                    winner_count INTEGER NOT NULL DEFAULT 1,
                    emoji TEXT,
                    ended INTEGER NOT NULL DEFAULT 0,
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaway_entries (
                    giveaway_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (giveaway_id, user_id)
                )
                """
            )
            conn.commit()

    def _schedule_timer(self, giveaway_id: int, ends_at: datetime) -> None:
        """Schedule a one-shot task that fires exactly at ends_at, so a
        giveaway locks/announces immediately instead of waiting for the
        next _check_expired_giveaways sweep (previously up to 30s late)."""
        self._cancel_timer(giveaway_id)
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        delay = (ends_at - datetime.now(timezone.utc)).total_seconds()
        self._timers[giveaway_id] = self.bot.loop.create_task(self._run_timer(giveaway_id, max(delay, 0.0)))

    def _cancel_timer(self, giveaway_id: int) -> None:
        task = self._timers.pop(giveaway_id, None)
        if task and not task.done() and task is not asyncio.current_task():
            # Guard against a timer cancelling itself: _finalize_giveaway_row
            # calls this too (so manual /giveaway end and the safety-net
            # sweep clean up a now-redundant timer), but when the precise
            # timer's own expiry is what triggered finalize, self-cancelling
            # here would throw CancelledError into the middle of announcing
            # winners.
            task.cancel()

    async def _run_timer(self, giveaway_id: int, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
                if not row or row["ended"] or row["cancelled"]:
                    return
                await self._finalize_giveaway_row(row, conn=conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            print(f"[GIVEAWAY TIMER] failed to finalize giveaway {giveaway_id}")
        finally:
            self._timers.pop(giveaway_id, None)

    @tasks.loop(seconds=30)
    async def _check_expired_giveaways(self) -> None:
        """Safety net only — the real-time path is _schedule_timer(). This
        catches anything whose timer never got (re)scheduled, e.g. a
        giveaway that was already active before the bot last restarted."""
        now = datetime.utcnow().replace(tzinfo=None)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM giveaways WHERE ended=0 AND cancelled=0",
                ).fetchall()
        except Exception:
            return

        for row in rows:
            gid = int(row["id"])
            ends_at = datetime.fromisoformat(row["ends_at"])
            if ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=timezone.utc)

            existing = self._timers.get(gid)
            if existing and not existing.done():
                continue  # already has a live precise timer, nothing to do

            if ends_at <= datetime.now(timezone.utc):
                # Already due and somehow has no timer running — finalize
                # right now instead of scheduling a 0s timer.
                try:
                    with self._connect() as conn:
                        current = conn.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
                        if not current or current["ended"] or current["cancelled"]:
                            continue
                        await self._finalize_giveaway_row(current, conn=conn)
                except Exception:
                    continue
            else:
                # Not due yet but missing its precise timer (e.g. right
                # after a restart) — (re)schedule it.
                self._schedule_timer(gid, ends_at)

    @_check_expired_giveaways.before_loop
    async def _before_check_expired_giveaways(self) -> None:
        await self.bot.wait_until_ready()

    def _build_embed(self, prize: str, winners: int, entries: int, ends_at: datetime, host: str, giveaway_id: int | None = None) -> discord.Embed:
        ts = int(ends_at.timestamp())
        embed = discord.Embed(
            title=f"🎉 Giveaway: {prize}",
            description=(
                "Click the button below to enter.\n\n"
                f"**Winners:** {winners}\n"
                f"**Entries:** {entries}\n"
                f"**Ends:** <t:{ts}:R> (<t:{ts}:f>)"
            ),
            color=COLOUR_INFO,
        )
        embed.set_footer(text=f"Hosted by {host}" + (f" • ID {giveaway_id}" if giveaway_id is not None else ""))
        embed.timestamp = ends_at
        return embed

    async def _post_giveaway_bundle(
        self,
        guild: discord.Guild,
        host: discord.User | discord.Member,
        target: discord.TextChannel | discord.Thread,
        prize: str,
        secs: int,
        winners_n: int,
        emoji_s: str,
    ) -> tuple[discord.Message | None, int | None, str | None]:
        ends = datetime.now(timezone.utc) + timedelta(seconds=secs)
        ends_naive = ends.replace(tzinfo=None)
        msg = None
        try:
            msg = await target.send(
                embed=self._build_embed(prize, winners_n, 0, ends, host.display_name),
                view=GiveawayEntryView(self.bot, emoji_s),
            )
        except discord.HTTPException as exc:
            return None, None, str(exc)[:200]

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO giveaways (
                    guild_id, channel_id, message_id, ends_at, prize, host_id, winner_count, emoji, ended, cancelled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    guild.id,
                    target.id,
                    msg.id,
                    ends_naive.isoformat(),
                    prize,
                    host.id,
                    winners_n,
                    emoji_s,
                ),
            )
            giveaway_id = cur.lastrowid
            conn.commit()

        self._schedule_timer(giveaway_id, ends)

        try:
            updated_embed = self._build_embed(prize, winners_n, 0, ends, host.display_name, giveaway_id)
            await msg.edit(embed=updated_embed, view=GiveawayEntryView(self.bot, emoji_s))
        except discord.HTTPException:
            pass
        return msg, giveaway_id, None

    async def _refresh_giveaway_message(self, giveaway_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
        if not row:
            return
        guild = self.bot.get_guild(int(row["guild_id"]))
        if not guild:
            return
        channel = guild.get_channel(int(row["channel_id"]))
        if not channel:
            try:
                channel = await guild.fetch_channel(int(row["channel_id"]))
            except Exception:
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            message = await channel.fetch_message(int(row["message_id"]))
        except Exception:
            return
        if not message.embeds:
            return
        with self._connect() as conn:
            entries_n = conn.execute(
                "SELECT COUNT(*) AS count FROM giveaway_entries WHERE giveaway_id=?",
                (giveaway_id,),
            ).fetchone()["count"]
        ends_at = datetime.fromisoformat(row["ends_at"])
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        host_user = self.bot.get_user(int(row["host_id"]))
        host_name = host_user.display_name if host_user else "Unknown"
        updated_embed = self._build_embed(
            row["prize"],
            int(row["winner_count"]),
            int(entries_n),
            ends_at,
            host_name,
            giveaway_id,
        )
        try:
            await message.edit(embed=updated_embed, view=GiveawayEntryView(self.bot, row["emoji"]))
        except discord.HTTPException:
            pass

    async def _enter_giveaway(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.message:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        with self._connect() as conn:
            giveaway = conn.execute(
                "SELECT * FROM giveaways WHERE guild_id=? AND channel_id=? AND message_id=?",
                (interaction.guild_id, interaction.channel_id, interaction.message.id),
            ).fetchone()
        if not giveaway or giveaway["cancelled"] or giveaway["ended"]:
            return await interaction.response.send_message(embed=err_embed("This giveaway is no longer active."), ephemeral=True)
        if interaction.user.bot:
            return await interaction.response.send_message("Bots cannot enter giveaways.", ephemeral=True)

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
                (giveaway["id"], interaction.user.id),
            ).fetchone()
            if existing:
                return await interaction.response.send_message(embed=err_embed("You are already entered in this giveaway."), ephemeral=True)
            conn.execute(
                "INSERT INTO giveaway_entries (giveaway_id, guild_id, user_id) VALUES (?, ?, ?)",
                (giveaway["id"], interaction.guild_id, interaction.user.id),
            )
            conn.commit()

        await interaction.response.send_message(
            embed=ok_embed("Giveaway entry confirmed", f"You are entered for **{giveaway['prize']}**."),
            ephemeral=True,
        )
        await self._refresh_giveaway_message(int(giveaway["id"]))

    async def _finalize_giveaway_row(self, row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> None:
        close_conn = False
        if conn is None:
            conn = self._connect()
            close_conn = True
        try:
            if int(row["ended"]) or int(row["cancelled"]):
                return
            conn.execute("UPDATE giveaways SET ended=1 WHERE id=?", (row["id"],))
            conn.commit()
            self._cancel_timer(int(row["id"]))

            entries = conn.execute(
                "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?",
                (row["id"],),
            ).fetchall()
            if entries:
                winners = [int(e["user_id"]) for e in entries]
                chosen = random.sample(winners, k=min(int(row["winner_count"]), len(winners)))
            else:
                chosen = []

            guild = self.bot.get_guild(int(row["guild_id"]))
            channel = None
            if guild:
                channel = guild.get_channel(int(row["channel_id"]))
                if not channel:
                    try:
                        channel = await guild.fetch_channel(int(row["channel_id"]))
                    except Exception:
                        channel = None
            if channel and isinstance(channel, (discord.TextChannel, discord.Thread)):
                if chosen:
                    mentions = ", ".join(
                        f"<@{user_id}>" for user_id in chosen
                    )
                    await channel.send(
                        content=f"🎉 **Giveaway complete** for **{row['prize']}** — winner(s): {mentions}",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                else:
                    await channel.send(content=f"🎉 **Giveaway complete** for **{row['prize']}** — no entries were received.")

            if guild:
                try:
                    message = await channel.fetch_message(int(row["message_id"]))
                except Exception:
                    message = None
                if message:
                    ends_at = datetime.fromisoformat(row["ends_at"])
                    if ends_at.tzinfo is None:
                        ends_at = ends_at.replace(tzinfo=timezone.utc)
                    host_user = self.bot.get_user(int(row["host_id"]))
                    host_name = host_user.display_name if host_user else "Unknown"
                    embed = self._build_embed(row["prize"], int(row["winner_count"]), len(entries), ends_at, host_name, int(row["id"]))
                    embed.description += "\n\n**Result:** " + (", ".join(f"<@{uid}>" for uid in chosen) if chosen else "No winners")
                    await message.edit(embed=embed, view=None)
        finally:
            if close_conn:
                conn.close()

    async def _giveaway_from_message_submit(self, interaction: discord.Interaction, modal: GiveawayFromMessageModal) -> None:
        src = modal._source
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Use this from a text channel or thread.", ephemeral=True)

        src_gid = src.guild.id if src.guild else None
        if src_gid is None or src_gid != interaction.guild_id:
            return await interaction.response.send_message("That message is not in this server.", ephemeral=True)

        secs = _parse_duration(modal.duration.value.strip())
        if secs is None or secs < MIN_SEC or secs > MAX_SEC:
            return await interaction.response.send_message(
                embed=err_embed("Invalid duration. Use e.g. `10m`, `1h`, `2d` — between 1 minute and 14 days."),
                ephemeral=True,
            )
        prize = modal.prize.value.strip()[:500]
        if not prize:
            return await interaction.response.send_message(embed=err_embed("Prize required."), ephemeral=True)

        raw_ch = (modal.channel_id.value or "").strip()
        target = interaction.channel
        if raw_ch:
            try:
                target_id = int(raw_ch)
            except ValueError:
                return await interaction.response.send_message(embed=err_embed("Invalid channel ID."), ephemeral=True)
            target = interaction.guild.get_channel(target_id)
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message(embed=err_embed("Pick a text channel or thread."), ephemeral=True)

        w_raw = (modal.winners.value or "").strip() or "1"
        try:
            winners_n = int(w_raw)
        except ValueError:
            return await interaction.response.send_message(embed=err_embed("Winners must be a number."), ephemeral=True)
        if winners_n < 1 or winners_n > 20:
            return await interaction.response.send_message(embed=err_embed("Winners must be between 1 and 20."), ephemeral=True)

        emoji_s = (modal.emoji.value or "").strip() or "🎉"
        emoji_s = emoji_s[:100]

        await interaction.response.defer(ephemeral=True)
        msg, new_id, err = await self._post_giveaway_bundle(interaction.guild, interaction.user, target, prize, secs, winners_n, emoji_s)
        if err or not msg or new_id is None:
            return await interaction.followup.send(embed=err_embed(f"Could not post giveaway. {err or 'Unknown error'}"), ephemeral=True)

        await interaction.followup.send(embed=ok_embed("Giveaway started", f"{msg.jump_url}\nID `{new_id}`"), ephemeral=True)

    @giveaway_group.command(name="start", description="Start a giveaway in this or another channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(duration="How long the giveaway runs (e.g. 1h, 30m, 2d)", prize="What you're giving away", channel="Where to post (default: current channel)", winners="How many winners (1–20)", emoji="Button emoji")
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        duration: str,
        prize: str,
        channel: discord.TextChannel | discord.Thread | None = None,
        winners: app_commands.Range[int, 1, 20] = 1,
        emoji: str | None = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        secs = _parse_duration(duration.strip())
        if secs is None or secs < MIN_SEC or secs > MAX_SEC:
            return await interaction.response.send_message(embed=err_embed("Invalid duration. Use e.g. `10m`, `1h`, `2d` — between 1 minute and 14 days."), ephemeral=True)
        prize_clean = prize.strip()[:500]
        if not prize_clean:
            return await interaction.response.send_message(embed=err_embed("Prize required."), ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message(embed=err_embed("Pick a text channel or thread."), ephemeral=True)
        emoji_s = (emoji or "🎉").strip()[:100]
        await interaction.response.defer(ephemeral=True)
        msg, new_id, err = await self._post_giveaway_bundle(interaction.guild, interaction.user, target, prize_clean, secs, int(winners), emoji_s)
        if err or not msg or new_id is None:
            return await interaction.followup.send(embed=err_embed(f"Could not post giveaway. {err or 'Unknown error'}"), ephemeral=True)
        await interaction.followup.send(embed=ok_embed("Giveaway started", f"{msg.jump_url}\nID `{new_id}`"), ephemeral=True)

    @giveaway_group.command(name="end", description="End a giveaway early and draw winners")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: str):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        try:
            gid = int(giveaway_id.strip())
        except ValueError:
            return await interaction.response.send_message(embed=err_embed("Invalid giveaway ID."), ephemeral=True)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM giveaways WHERE id=? AND guild_id=?", (gid, interaction.guild_id)).fetchone()
        if not row:
            return await interaction.response.send_message(embed=err_embed("No matching giveaway for that ID."), ephemeral=True)
        if int(row["ended"]) or int(row["cancelled"]):
            return await interaction.response.send_message(embed=err_embed("That giveaway already ended."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        with self._connect() as conn:
            current = conn.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
            await self._finalize_giveaway_row(current, conn=conn)
        await interaction.followup.send(embed=ok_embed("Giveaway ended", f"Results were posted for giveaway ID `{gid}`."), ephemeral=True)

    @giveaway_group.command(name="cancel", description="Cancel a giveaway without drawing winners")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_cancel(self, interaction: discord.Interaction, message_id: str, channel: discord.TextChannel | discord.Thread | None = None):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        try:
            mid = int(message_id.strip())
        except ValueError:
            return await interaction.response.send_message(embed=err_embed("Invalid message ID."), ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message(embed=err_embed("Invalid channel."), ephemeral=True)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM giveaways WHERE guild_id=? AND channel_id=? AND message_id=?",
                (interaction.guild_id, target.id, mid),
            ).fetchone()
        if not row or int(row["ended"]) or int(row["cancelled"]):
            return await interaction.response.send_message(embed=err_embed("No active giveaway for that message."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        with self._connect() as conn:
            conn.execute("UPDATE giveaways SET cancelled=1 WHERE id=?", (row["id"],))
            conn.commit()
        self._cancel_timer(int(row["id"]))
        try:
            channel = interaction.guild.get_channel(target.id)
            if channel and isinstance(channel, (discord.TextChannel, discord.Thread)):
                message = await channel.fetch_message(mid)
                embed = message.embeds[0].copy() if message.embeds else discord.Embed()
                embed.description = (embed.description or "") + "\n\n**Cancelled**"
                await message.edit(embed=embed, view=None)
        except Exception:
            pass
        await interaction.followup.send(embed=ok_embed("Giveaway cancelled", "The message was updated."), ephemeral=True)

    @giveaway_group.command(name="reroll", description="Draw new winners from the same giveaway entries")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_reroll(self, interaction: discord.Interaction, giveaway_id: str):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        try:
            gid = int(giveaway_id.strip())
        except ValueError:
            return await interaction.response.send_message(embed=err_embed("Invalid giveaway ID."), ephemeral=True)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM giveaways WHERE id=? AND guild_id=?", (gid, interaction.guild_id)).fetchone()
        if not row:
            return await interaction.response.send_message(embed=err_embed("No giveaway record for that ID."), ephemeral=True)
        if not int(row["ended"]) or int(row["cancelled"]):
            return await interaction.response.send_message(embed=err_embed("End the giveaway first."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        with self._connect() as conn:
            entries = conn.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,)).fetchall()
        if not entries:
            return await interaction.followup.send(embed=err_embed("No entrants to reroll."), ephemeral=True)
        winners = [int(e["user_id"]) for e in entries]
        chosen = random.sample(winners, k=min(int(row["winner_count"]), len(winners)))
        channel = interaction.guild.get_channel(int(row["channel_id"]))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.followup.send(embed=err_embed("Giveaway channel not found."), ephemeral=True)
        mentions = ", ".join(f"<@{uid}>" for uid in chosen)
        await channel.send(content=f"🎉 **Reroll** for **{row['prize']}** — new winner(s): {mentions}", allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send(embed=ok_embed("Reroll complete", mentions), ephemeral=True)

    @giveaway_group.command(name="list", description="List active giveaways in this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM giveaways WHERE guild_id=? AND ended=0 AND cancelled=0 ORDER BY ends_at ASC",
                (interaction.guild_id,),
            ).fetchall()
        if not rows:
            return await interaction.response.send_message(embed=discord.Embed(title="Giveaways", description="No active giveaways.", color=COLOUR_INFO), ephemeral=True)
        lines = []
        for row in rows[:20]:
            ch = interaction.guild.get_channel(int(row["channel_id"]))
            loc = f"<#{row['channel_id']}>" if ch else f"`{row['channel_id']}`"
            ends_at = datetime.fromisoformat(row["ends_at"])
            if ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=timezone.utc)
            ts = int(ends_at.timestamp())
            lines.append(f"• **#{row['id']}** — {row['prize'][:80]}{'…' if len(row['prize']) > 80 else ''}\n  {loc} · ends <t:{ts}:R>")
        embed = discord.Embed(title="Active giveaways", description="\n".join(lines), color=COLOUR_INFO)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def handle_giveaway_context_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Use this in a text channel or thread.", ephemeral=True)
        if message.guild is None or message.guild.id != interaction.guild_id:
            return await interaction.response.send_message("That message is not in this server.", ephemeral=True)
        await interaction.response.send_modal(GiveawayFromMessageModal(self, message))


@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.context_menu(name="Start giveaway from message")
async def giveaway_from_message_context(interaction: discord.Interaction, message: discord.Message) -> None:
    cog = interaction.client.get_cog("Giveaways")
    if not isinstance(cog, GiveawayCog):
        return await interaction.response.send_message("Giveaways unavailable.", ephemeral=True)
    await cog.handle_giveaway_context_menu(interaction, message)


class GiveawayEntryView(discord.ui.View):
    def __init__(self, bot: commands.Bot, emoji: str | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.button_emoji = emoji or "🎉"
        self.enter_button.emoji = self._resolve_emoji(self.button_emoji)

    def _resolve_emoji(self, emoji: str | None) -> Any:
        if not emoji:
            return "🎉"
        if isinstance(emoji, str) and emoji.startswith("<") and ":" in emoji:
            try:
                return discord.PartialEmoji.from_str(emoji)
            except Exception:
                return "🎉"
        return emoji

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.secondary, custom_id="vortex_giveaway_enter")
    async def enter_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        cog = self.bot.get_cog("Giveaways")
        if not isinstance(cog, GiveawayCog):
            return await interaction.response.send_message("Giveaways unavailable.", ephemeral=True)
        await cog._enter_giveaway(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))
    bot.tree.add_command(giveaway_from_message_context)