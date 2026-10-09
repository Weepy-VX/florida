"""
main.py — Runs the main FSRP bot only.
"""
import asyncio
import os
import time

os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_NO_DM_TRACEBACK"] = "True"
os.environ["JISHAKU_HIDE"] = "True"

import discord
from discord.ext import commands
import utils.helpers as helpers
from utils.db import init_db, migrate_db

helpers.START_TIME = time.time()

# Department bots are disabled for now.
# DEPT_NAMES = ("mpd", "mdso", "fhp", "mdfr")
#
# def start_dept_bots() -> dict[str, subprocess.Popen]:
#     """Spawn department bots as subprocesses. Returns a dept->Popen mapping."""
#     from configs.dept import DEPT_TOKENS
#
#     procs: dict[str, subprocess.Popen] = {}
#     for dept in DEPT_NAMES:
#         token = (DEPT_TOKENS.get(dept) or "").strip()
#         if not token:
#             print(f"[MAIN]   ⏭ Skipping {dept.upper()} (no token in configs/dept_tokens.py)")
#             continue
#         env = os.environ.copy()
#         env["DEPT"] = dept
#         proc = subprocess.Popen(
#             [sys.executable, "-m", "fsrpbot.dept_main"],
#             env=env,
#             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
#             stdout=sys.stdout,
#             stderr=sys.stderr,
#             start_new_session=os.name != "nt",  # new process group so signals are delivered
#         )
#         procs[dept] = proc
#         print(f"[MAIN]   ▶ Started {dept.upper()} (pid {proc.pid})")
#     return procs


def make_bot() -> commands.Bot:
    intents = discord.Intents.all()
    return commands.Bot(command_prefix="-", intents=intents)


async def setup_main_bot(bot: commands.Bot):
    from configs import main as cfg

    MAIN_COGS = [
        "jishaku",
        "cogs.main.general",
        "cogs.main.erlc",
        "cogs.main.sessions",
        "cogs.main.infractions",
        "cogs.main.staff",
        "cogs.main.feedback",
        "cogs.main.dynamic_roles",
        "cogs.main.tickets",
        "cogs.main.giveaway",
        "cogs.main.rank_sync",
        "cogs.main.queue",
        "cogs.main.autoreply",
        "cogs.main.embed_retriever"
        # "cogs.sales_tracker",
    ]

    async def setup_hook():
        guild = discord.Object(id=cfg.GUILD_ID)

        # 1. Copy everything currently registered globally (populated when
        #    the cogs above were loaded) into this guild's command list,
        #    and push that guild-scoped copy to Discord FIRST — this has
        #    to happen before the global list is cleared below, or there's
        #    nothing left to copy.
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)

        # 2. Now that the guild has its own copy, wipe the global list and
        #    push that empty list too, so Discord stops showing a second,
        #    globally-registered set of the same commands alongside the
        #    guild ones (this is what caused the duplicates).
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()

        print("[MAIN]   ✔ Commands synced to the server successfully!")
        print(len(bot.cogs))
        print(bot.cogs.keys())

    # This actually has to be assigned onto the bot instance for discord.py
    # to call it — previously it was just a nested function sitting in local
    # scope, so the clear/resync above never ran, and old globally-synced
    # commands kept sitting alongside the guild-synced ones as duplicates.
    bot.setup_hook = setup_hook

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

    for cog in MAIN_COGS:
        await bot.load_extension(cog)
        print(f"[MAIN]   ✔ Loaded {cog}")



async def main():
    init_db()
    migrate_db()

    from configs import main as main_cfg

    # Department bots are disabled for now.
    main_bot = make_bot()
    await setup_main_bot(main_bot)

    async with main_bot:
        await main_bot.start(main_cfg.BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass