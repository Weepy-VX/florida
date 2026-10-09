"""
cogs/main/tickets.py — Ticket channels, control buttons, form panels, and transcripts.
"""
import asyncio
import base64
import copy
import html
import io
import json
import os
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from configs.main import COLOUR_BRAND, COLOUR_ERROR, GUILD_ID

try:
    import chat_exporter
except ImportError:  # pragma: no cover - optional dependency fallback
    chat_exporter = None

WEB_SECRET = os.environ.get("WEB_SECRET", "change-me")
PORT = int(os.environ.get("PORT", "12574"))
DB_PATH = Path(__file__).with_name("ticket_dashboard.json")

print(f"[TICKETS] loaded from {__file__}")
def load_db() -> dict:
    if DB_PATH.exists():
        try:
            data = json.loads(DB_PATH.read_text())
            data.setdefault("panels", {})
            data.setdefault("multi_panels", {})
            data.setdefault("settings", {"prefix": "-", "transcript_channel_id": ""})
            data["settings"].setdefault("guild_colors", {})
            data.setdefault("ticket_state", {})
            return data
        except json.JSONDecodeError:
            pass
    return {
        "panels": {},
        "multi_panels": {},
        # "guild_colors" maps a guild ID (string) to an int color, used for
        # that server's ticket form-answer embeds. Falls back to
        # COLOUR_BRAND for any guild that hasn't set one.
        "settings": {"prefix": "-", "transcript_channel_id": "", "guild_colors": {}},
        "prefix": "-",
        # Per-channel state (currently just who claimed each ticket), keyed
        # by channel ID as a string, so the closing summary embed can show
        # an accurate "Assisted By" even after a bot restart.
        "ticket_state": {},
    }


def save_db(data: dict) -> None:
    DB_PATH.write_text(json.dumps(data, indent=2))


def get_guild_color(guild_id: Optional[int]) -> int:
    """Per-server color for ticket form-answer embeds, set via /ticketcolor.
    Falls back to the bot's global COLOUR_BRAND if this guild hasn't set one.
    """
    if guild_id is not None:
        raw = db.get("settings", {}).get("guild_colors", {}).get(str(guild_id))
        if isinstance(raw, int):
            return raw
    return COLOUR_BRAND


def set_guild_color(guild_id: int, color: int) -> None:
    db.setdefault("settings", {}).setdefault("guild_colors", {})[str(guild_id)] = color
    save_db(db)


def parse_json_field(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_embed_from_panel(panel_data: dict, modal_items=None) -> discord.Embed:
    raw_json = panel_data.get("message_json", "")
    payload = parse_json_field(raw_json) or {}

    embed_dict = {}
    if "embeds" in payload and payload["embeds"]:
        embed_dict = payload["embeds"][0]
    elif "title" in payload or "description" in payload:
        embed_dict = payload

    embed = discord.Embed.from_dict(embed_dict) if embed_dict else discord.Embed(color=COLOUR_BRAND)

    if modal_items:
        for item in modal_items:
            if isinstance(item, discord.ui.TextInput):
                field_name = getattr(item, "_original_label", "Question")
                embed.add_field(name=field_name, value=item.value or "", inline=False)
            elif isinstance(item, discord.ui.Select):
                val = ", ".join(item.values) if item.values else "No selection made"
                embed.add_field(name=getattr(item, "_original_label", "Selection"), value=val, inline=False)

    return embed


db = load_db()

SUPPORT_ROLE_ID = 1530738492047360124


def _format_duration(seconds: float) -> str:
    """Format an elapsed-time delta as e.g. '9h, 47m, 2s'."""
    total = int(max(seconds, 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h, {minutes}m, {secs}s"


def _parse_opener_id(topic: Optional[str]) -> Optional[int]:
    match = re.search(r"User ID:\s*(\d+)", topic or "")
    return int(match.group(1)) if match else None


def _parse_panel_id(topic: Optional[str]) -> Optional[str]:
    match = re.search(r"Panel ID:\s*([^|]+)", topic or "")
    return match.group(1).strip() if match else None


def _build_channel_name(format_str: str, member_name: str, existing_names: set) -> str:
    """Shared channel-name builder used both when a ticket is first created
    and when it's escalated to a different panel."""
    channel_name = (format_str or "ticket-{user}").lower().replace("{user}", member_name)
    channel_name = re.sub(r"[^a-z0-9-]", "-", channel_name)
    channel_name = re.sub(r"-+", "-", channel_name).strip("-")[:80]
    if not channel_name:
        channel_name = "ticket"
    if channel_name in existing_names:
        channel_name = f"{channel_name}-{random.randint(1000, 9999)}"
    return channel_name


def build_closing_summary_embed(
    *,
    guild: discord.Guild,
    channel: discord.TextChannel,
    panel_data: dict,
    reason: str,
    opener: Optional[discord.abc.User],
    opener_id: Optional[int],
    assisted_by: Optional[discord.abc.User],
    closed_by: Optional[discord.abc.User] = None,
) -> discord.Embed:
    """Build the 'Thank you for contacting ... Support' summary embed sent
    alongside the HTML transcript when a ticket is closed."""
    embed = discord.Embed(
        description=(
            f"We appreciate you taking the time to contact **{guild.name}**'s Support Team. "
            "You are more than welcome to leave a review on the Support Team member who assisted "
            "you utilizing our `/feedback` command."
        ),
        color=COLOUR_BRAND,
    )
    embed.set_author(
        name=f"Thank you for contacting {guild.name} Support",
        icon_url=guild.icon.url if guild.icon else None,
    )

    embed.add_field(name="Support Type", value=panel_data.get("name") or "Support", inline=True)
    embed.add_field(name="Close Reason", value=reason or "No reason provided", inline=True)
    embed.add_field(
        name="Assisted By",
        value=assisted_by.mention if assisted_by else "*Not claimed*",
        inline=True,
    )
    embed.add_field(
        name="Closed By",
        value=closed_by.mention if closed_by else "Unknown",
        inline=True,
    )

    opener_name = getattr(opener, "name", None) or (str(opener_id) if opener_id else "Unknown")
    opener_id_display = opener.id if opener else (opener_id or "Unknown")
    footer_icon = opener.display_avatar.url if opener else None
    embed.set_footer(text=f"{opener_name} | {opener_id_display}", icon_url=footer_icon)

    return embed


async def _deliver_transcript(
    bot: commands.Bot,
    guild: discord.Guild,
    channel: discord.TextChannel,
    panel_data: dict,
    reason: str,
    closed_by: discord.abc.User,
) -> None:
    """Export a ticket's transcript, then send the HTML file + summary embed
    (no message content) to the configured transcript log channel, and
    best-effort DM the same embed + file to the ticket opener.
    """
    transcript_channel_id = panel_data_transcript_channel_id(panel_data)
    if not (transcript_channel_id and str(transcript_channel_id).isdigit()):
        print("[TRANSCRIPT WARN] No transcript_channel_id set for this panel or in global settings.")
        return

    try:
        transcript = await export_transcript(channel)
    except Exception as exc:
        print(f"[TRANSCRIPT ERROR] export_transcript failed: {exc}")
        return

    if not transcript:
        print("[TRANSCRIPT ERROR] export_transcript returned empty content.")
        return

    state = db.get("ticket_state", {}).get(str(channel.id), {})
    claimed_by_id = state.get("claimed_by")
    assisted_by = guild.get_member(claimed_by_id) if claimed_by_id else closed_by
    opener_id = _parse_opener_id(channel.topic)
    opener = guild.get_member(opener_id) if opener_id else None

    summary_embed = build_closing_summary_embed(
        guild=guild,
        channel=channel,
        panel_data=panel_data,
        reason=reason,
        opener=opener,
        opener_id=opener_id,
        assisted_by=assisted_by,
        closed_by=closed_by,
    )

    transcript_bytes = transcript.encode("utf-8")
    filename = f"transcript-{channel.name}.html"

    transcript_channel = bot.get_channel(int(transcript_channel_id))
    if transcript_channel:
        try:
            # File + embed only — no message content.
            await transcript_channel.send(
                embed=summary_embed,
                file=discord.File(io.BytesIO(transcript_bytes), filename=filename),
            )
        except Exception as exc:
            print(f"[TRANSCRIPT ERROR] Failed sending to transcript channel: {exc}")
    else:
        print(f"[TRANSCRIPT ERROR] Channel {transcript_channel_id} not found/cached.")

    # Best-effort DM to the ticket opener. A fresh File/embed is built for
    # this send since a File's underlying BytesIO is consumed after one
    # send call. Many users have DMs closed to the bot, so failures here
    # are swallowed rather than blocking the close.
    if opener:
        try:
            await opener.send(
                embed=summary_embed,
                file=discord.File(io.BytesIO(transcript_bytes), filename=filename),
            )
        except Exception as exc:
            print(f"[TRANSCRIPT DM] Could not DM transcript to opener {opener}: {exc}")


# The fixed escalation notice sent whenever a ticket is moved to a new panel.
ESCALATION_MESSAGE_PAYLOAD = {
    "embeds": [
        {
            "color": 15437332,
            "title": "<:community:1531888445075619873>  Ticket Escalated",
            "description": (
                "Your ticket has been escalated to the team that is best able to assist you "
                "with your inquiry. Please wait patiently for our team to assist you."
            ),
            "thumbnail": {"url": "https://i.imgur.com/9KCE36G.png"},
            "image": {"url": "https://i.imgur.com/y13i2M7.png"},
        }
    ]
}


TICKET_CATEGORIES = [
    1531417119575773435,  # Support
    1531465336178741340,  # Report
    1531465375537958955,  # Leadership
]

# ── New select-menu triage system (Assistance channel) ──────────────────
# Role/category IDs pulled straight from the pasted Components V2 payloads.
GENERAL_INQUIRY_ROLE_ID = 1530738492047360124
REPORT_TICKET_ROLE_ID = 1530738492064403532
PURCHASE_TICKET_ROLE_ID = 1530738492076851313

GENERAL_INQUIRY_CATEGORY_ID = TICKET_CATEGORIES[0]  # Support
REPORT_TICKET_CATEGORY_ID = TICKET_CATEGORIES[1]  # Report
PURCHASE_TICKET_CATEGORY_ID = 1532141840382300212

# How confident the auto-reply embedding match needs to be before it's
# even considered for the General Inquiry triage flow, and the higher bar
# at/above which the bot is confident enough to skip the "Need more help"
# escape-hatch buttons entirely. Tune these two constants to adjust
# behaviour without touching the flow logic below.
TRIAGE_MATCH_FLOOR = 0.60
TRIAGE_CONFIDENT_THRESHOLD = 0.80

# Anchors used to semantically classify the "Proof?" answer on report
# tickets. The report is voided only when the answer's closest cluster is
# the void cluster AND that similarity clears VOID_CONFIDENCE — anything
# ambiguous defaults to accepted, matching the requested behaviour
# ("anything that isn't a no / witnesses-only gets accepted").
REPORT_PROOF_VOID_ANCHORS = [
    "No, I don't have any proof.",
    "No proof.",
    "I don't have any evidence.",
    "Only witnesses saw it happen.",
    "Just witnesses, no recording or screenshots.",
    "There were only witnesses, nothing recorded.",
    "N/A",
    "None.",
    "No"
]
REPORT_PROOF_ACCEPT_ANCHORS = [
    "Yes, I have proof.",
    "Yes, I recorded it.",
    "I have a screenshot.",
    "Here is the video link.",
    "I have evidence attached.",
    "Yes.",
]
REPORT_PROOF_VOID_CONFIDENCE = 0.80

GENERAL_INQUIRY_WELCOME_TEMPLATE = {
    "content": "{user.mention}<@&1530738492047360124>",
    "embeds": [
        {"color": 16096811, "image": {"url": "https://i.imgur.com/dhveVDN.png"}},
        {
            "color": 16096811,
            "thumbnail": {"url": "https://i.imgur.com/9KCE36G.png"},
            "image": {"url": "https://i.imgur.com/y13i2M7.png"},
            "description": (
                "## <:notes:1531825297798725682> General Inquiry\n### Hey {user.mention}!\n"
                "Thank you for reaching out to our Support Team! We\u2019re here to help with any "
                "questions you have about the Discord or in-game server or general information.\n\n"
                "Our support team has been notified so please **refrain** from pinging any support "
                "team members or your ticket may be closed. While you wait, if you remember anything "
                "else you need help with, feel free to send it below so our team can stay up to date "
                "with all the details.\n\nWe appreciate your patience and will be with you shortly!"
            ),
        },
        {
            "color": 16096811,
            "fields": [{"name": "Please state your inquiry:", "value": "", "inline": False}],
        },
    ],
}

REPORT_TICKET_WELCOME_TEMPLATE = {
    "content": "{user.mention} <@&1530738492064403532>",
    "username": "Florida State Systems",
    "avatar_url": "https://cdn.discordapp.com/avatars/1456905308814442506/de24e917eb8d0c30b9169ee329561e59.png?size=1024",
    "embeds": [
        {"color": 16096811, "image": {"url": "https://i.imgur.com/dhveVDN.png"}},
        {
            "color": 16096811,
            "thumbnail": {"url": "https://i.imgur.com/9KCE36G.png"},
            "image": {"url": "https://i.imgur.com/y13i2M7.png"},
            "description": (
                "## <:gavel:1531869255476314253> Report Ticket\n### Hey {user.mention}!\n"
                "Thank you for reaching out to our Internal Affairs Team! We\u2019re here to address "
                "your concerns regarding our staff. Please be sure to provide any and all clip or "
                "other such evidence for our IA team to review. Please note that should you not have "
                "proof, your report will be voided.\n\nOur IA team has been notified so please "
                "**refrain** from pinging any IA members or your ticket may be closed. While you wait, "
                "if you remember anything else you may wish to include, feel free to send it below so "
                "our team can stay up to date with all the details.\n\nWe appreciate your patience and "
                "will be with you shortly!"
            ),
        },
        {
            "color": 16096811,
            "fields": [
                {"name": "Reported Staff Member:", "value": "", "inline": False},
                {"name": "Brief Description of the Issue:", "value": "", "inline": False},
                {"name": "Proof?", "value": "", "inline": False},
            ],
        },
    ],
}

PURCHASE_TICKET_WELCOME_TEMPLATE = {
    "content": "{user.mention}<@&1530738492076851313>",
    "embeds": [
        {"color": 16096811, "image": {"url": "https://i.imgur.com/dhveVDN.png"}},
        {
            "color": 16096811,
            "thumbnail": {"url": "https://i.imgur.com/9KCE36G.png"},
            "image": {"url": "https://i.imgur.com/y13i2M7.png"},
            "description": (
                "## <:card:1535048863281451129> Purchase Ticket\n### Hey {user.mention}!\n"
                "Thank you for opening a purchase ticket!\n\nWe are here to assist you with your "
                "transaction, payment, or other related questions. Our Ownership team has been "
                "notified, so please **refrain** from pinging any of them to avoid your ticket being "
                "closed. Please have any necessary proof ready when opening tickets to redeem so our "
                "billing team can process and resolve your issue efficiently. We appreciate your "
                "patience and will be with you shortly!\n"
            ),
        },
        {
            "color": 16096811,
            "fields": [{"name": "Please state your inquiry:", "value": "", "inline": False}],
        },
    ],
}

TRIAGE_PANELS = {
    "general_inquiry": {
        "name": "General Inquiry",
        "type": "general_inquiry",
        "category_id": str(GENERAL_INQUIRY_CATEGORY_ID),
        "role_id": str(GENERAL_INQUIRY_ROLE_ID),
        "channel_name_format": "general-{user}",
    },
    "report_ticket": {
        "name": "Report Ticket",
        "type": "report_ticket",
        "category_id": str(REPORT_TICKET_CATEGORY_ID),
        "role_id": str(REPORT_TICKET_ROLE_ID),
        "channel_name_format": "report-{user}",
    },
    "purchase_ticket": {
        "name": "Purchase Ticket",
        "type": "purchase_ticket",
        "category_id": str(PURCHASE_TICKET_CATEGORY_ID),
        "role_id": str(PURCHASE_TICKET_ROLE_ID),
        "channel_name_format": "purchase-{user}",
    },
}

FAQ_TEXT = (
    "## Frequently Asked Questions (FAQs)\n"
    "### Q: How can I check why I was moderated in the in-game server?\n"
    "> A: You can view your moderation logs at https://melonly.xyz/my/logs/7488018762264743936. If "
    "you believe the punishment was unfair or false, you must report the staff member.\n"
    "### Q: Where can I submit an in-game ban appeal?\n"
    "> A: You can fill out an in-game ban appeal at https://melon.ly/form/7491272730407342080.\n"
    "### Q: Where can I submit a Discord server ban appeal?\n"
    "> A: Discord server ban appeals can be submitted at https://banappeal.gg/fla.\n"
    "### Q: How do I apply to join the staff team?\n"
    "> A: Staff applications are currently open. You can apply at "
    "https://discord.com/channels/1530738492018135171/1530738492626309312.\n"
    "### Q: Is this server voice chat (VC) only?\n"
    "> A: No, using voice chat is completely optional. However, whitelisted members must join the "
    "designated RTO channel for their active in-game roleplay role (except Civilians), though "
    "speaking is not required.\n"
    "### Q: Is the server online 24/7?\n"
    "> A: No. The server runs on a session-by-session basis and automatically shuts down when the "
    "player count drops too low.\n"
    "### Q: I won a giveaway. How do I claim my Robux?\n"
    "> A: Open a support ticket within the specific server where the giveaway was hosted. This "
    "server does not provide the funding and cannot pay out the prize.\n"
    "### Q: Can I buy the server or a staff rank?\n"
    "> A: No. Purchasing a staff rank is prohibited, and buying the server is strictly forbidden "
    "under the Discord Terms of Service.\n"
    "### Q: Can I report someone for behavior that occurred in Direct Messages (DMs)?\n"
    "> A: You cannot report standard community members for DM activity. However, you can report "
    "staff members or whitelisted members for DM behavior by opening a report ticket.\n"
    "### Q: How do I become a whitelisted member or join a whitelisted department?\n"
    "> A: Join any department listed in "
    "https://discord.com/channels/1530738492018135171/1530738492626309311 and complete their "
    "respective application.\n"
    "### Q: Where can I find the server rules and restrictions?\n"
    "> A: Our Discord and in-game rules are located at "
    "https://discord.com/channels/1530738492018135171/1530738492446081093. This link also details "
    "restricted guns and vehicles, which Server Boosters and VIPs may bypass as a perk.\n"
    "### Q: Does the community issue server-wide blacklists?\n"
    "> A: No, server blacklists are not utilized."
)

ASSISTANCE_INTRO_TEXT = (
    "> Welcome to our assistance channel. You can open a ticket below to get in touch with our team "
    "for assistance. We highly recommend reviewing our documentation and FAQs before creating a "
    "ticket."
)
ASSISTANCE_OPTIONS_TEXT = (
    "## <:notes:1531825297798725682>  General Inquiries\n"
    "> Select this option if you need assistance with everyday questions, community feedback, or "
    "general help. Our front-line staff handles these tickets to address standard concerns, answer "
    "server questions, or guide you in the right direction.\n"
    "## <:gavel:1531869255476314253> Report Tickets\n"
    "> Select this option to file a formal complaint regarding staff behavior or policy violations. "
    "Our Internal Affairs Team reviews these cases directly. To ensure a valid investigation, you "
    "must provide clear, undeniable evidence (such as screenshots or video) upon opening the ticket, "
    "or your report will be closed immediately.\n"
    "## <:card:1535048863281451129> Purchase Redemptions\n"
    "> Select this option for all transaction-related support, including missing packages, payment "
    "issues, or item redemptions. Please have any necessary proof ready when opening tickets to "
    "redeem so our  team can process and resolve your issue efficiently."
)


def _substitute_mentions(value, mention: str):
    """Recursively replace the `{user.mention}` placeholder anywhere inside
    a template's strings, dicts, or lists. Kept generic (rather than only
    patching `content`/`description`) so future template tweaks can drop
    the placeholder into any string field without extra plumbing."""
    if isinstance(value, str):
        return value.replace("{user.mention}", mention)
    if isinstance(value, dict):
        return {k: _substitute_mentions(v, mention) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_mentions(v, mention) for v in value]
    return value


def _build_triage_welcome(template: dict, mention: str, field_values: list) -> dict:
    """Deep-copies a welcome template, fills in `{user.mention}`, and sets
    the last embed's field values in order (these hold the user's modal
    answers, which can't be templated as plain string placeholders since
    they're arbitrary user input)."""
    payload = _substitute_mentions(copy.deepcopy(template), mention)
    fields = payload["embeds"][-1]["fields"]
    for field, value in zip(fields, field_values):
        field["value"] = (value or "Not provided")[:1024]
    return payload


def _get_autoreply_manager(bot: commands.Bot):
    """Reaches into the SemanticAutoReplyCog (autoreply.py) for its shared
    embedding manager, so the ticket triage flow reuses the exact same
    OpenRouter-backed AI matching the auto-reply system already runs on
    every message — no separate model/config to keep in sync."""
    cog = bot.get_cog("SemanticAutoReplyCog")
    return getattr(cog, "manager", None) if cog else None


async def _assess_report_proof(manager, answer: str) -> bool:
    """Returns True if the report should be accepted, False if it should be
    voided. A link is always accepted outright. Otherwise this leans on the
    autoreply system's embedding model to semantically compare the answer
    against void vs. accept anchor phrases, defaulting to accept whenever
    the signal is anything other than a clear no/witnesses-only, per spec.
    """
    text = (answer or "").strip()
    if not text:
        return False
    if re.search(r"https?://\S+|www\.\S+|discord\.gg/\S+", text, re.IGNORECASE):
        return True

    if manager is None:
        lowered = text.lower().strip(" .!")
        void_words = {"no", "none", "n/a", "na", "nope", "nah"}
        only_witnesses = "witness" in lowered and not re.search(r"http", lowered)
        return not (lowered in void_words or only_witnesses)

    try:
        embeddings = await manager._get_embeddings(
            [text] + REPORT_PROOF_VOID_ANCHORS + REPORT_PROOF_ACCEPT_ANCHORS
        )
    except Exception as exc:
        print(f"[TRIAGE AI ERROR] proof embedding call failed: {exc}")
        embeddings = None

    if not embeddings:
        return True  # Default-accept if the AI call itself fails.

    answer_vec = embeddings[0]
    void_vecs = embeddings[1 : 1 + len(REPORT_PROOF_VOID_ANCHORS)]
    accept_vecs = embeddings[1 + len(REPORT_PROOF_VOID_ANCHORS):]
    void_score = max(manager._cosine_similarity(answer_vec, v) for v in void_vecs)
    accept_score = max(manager._cosine_similarity(answer_vec, v) for v in accept_vecs)

    return not (void_score > accept_score and void_score >= REPORT_PROOF_VOID_CONFIDENCE)


DEFAULT_PANELS = {
    "support": {
        "name": "Support",
        "type": "support",
        "button_text": "Open Support Ticket",
        "category_id": str(TICKET_CATEGORIES[0]),
        "role_id": str(SUPPORT_ROLE_ID),
        "deploy_channel_id": "",
        "welcome_message": "Welcome, a staff member will be with you shortly.",
        "pings": [f"<@&{SUPPORT_ROLE_ID}>"],
        "questions": [
            {"type": "text", "label": "What do you need help with?", "required": True},
            {"type": "paragraph", "label": "Please add any details.", "required": False},
        ],
    },
}


class TicketControlView(discord.ui.View):
    def __init__(self, bot: commands.Bot, panel_id: str, panel_data: dict):
        super().__init__(timeout=None)
        self.bot = bot
        self.panel_id = panel_id
        self.panel_data = panel_data or {}

        claim_btn = discord.ui.Button(label="Claim", style=discord.ButtonStyle.secondary, custom_id=f"claim_btn_{panel_id}")
        claim_btn.callback = self.claim_callback
        close_btn = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, custom_id=f"close_btn_{panel_id}")
        close_btn.callback = self.close_callback

        self.add_item(claim_btn)
        self.add_item(close_btn)

    async def claim_callback(self, interaction: discord.Interaction):
        role_id = self.panel_data.get("role_id")
        if role_id and str(role_id).isdigit():
            role = interaction.guild.get_role(int(role_id))
            if role and role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
                return await interaction.response.send_message("You do not have permission to claim this.", ephemeral=True)

        # Claim/Unclaim is now a single toggling button. Whoever claimed it
        # (or an admin) can click it again to unclaim, freeing it back up.
        state = db.setdefault("ticket_state", {}).setdefault(str(interaction.channel.id), {})
        claimed_by = state.get("claimed_by")

        claim_button = discord.utils.get(self.children, custom_id=f"claim_btn_{self.panel_id}")

        if claimed_by:
            if claimed_by != interaction.user.id and not interaction.user.guild_permissions.administrator:
                return await interaction.response.send_message(
                    "Only the staff member who claimed this ticket (or an admin) can unclaim it.",
                    ephemeral=True,
                )
            state["claimed_by"] = None
            save_db(db)
            if claim_button:
                claim_button.label = "Claim"
            embed = discord.Embed(description=f"🔓 {interaction.user.mention} has unclaimed this ticket.", color=COLOUR_BRAND)
        else:
            state["claimed_by"] = interaction.user.id
            save_db(db)
            if claim_button:
                claim_button.label = "Unclaim"
            embed = discord.Embed(description=f"🔒 {interaction.user.mention} has claimed this ticket.", color=COLOUR_BRAND)

        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def close_callback(self, interaction: discord.Interaction):
        role_id = self.panel_data.get("role_id")
        if role_id and str(role_id).isdigit():
            role = interaction.guild.get_role(int(role_id))
            if role and role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
                return await interaction.response.send_message("You do not have permission to close this.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        await _deliver_transcript(
            bot=self.bot,
            guild=interaction.guild,
            channel=interaction.channel,
            panel_data=self.panel_data,
            reason="Resolved",
            closed_by=interaction.user,
        )

        db.get("ticket_state", {}).pop(str(interaction.channel.id), None)
        save_db(db)

        try:
            await interaction.channel.delete(reason=f"Closed via button by {interaction.user}")
        except discord.NotFound:
            pass


class CloseRequestView(discord.ui.View):
    """Sent by /closerequest. Lets the ticket opener (or staff with panel
    access) confirm or cancel a pending close request. Registered once as a
    single persistent view with fixed custom_ids (no per-instance panel
    data baked in) so it keeps working across bot restarts — all context
    (which panel, who opened the ticket) is resolved live from the channel
    each time a button is pressed, via the Tickets cog.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

        confirm_btn = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger, custom_id="close_request_confirm")
        confirm_btn.callback = self.confirm_callback
        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="close_request_cancel")
        cancel_btn.callback = self.cancel_callback

        self.add_item(confirm_btn)
        self.add_item(cancel_btn)

    def _tickets_cog(self):
        return self.bot.get_cog("Tickets")

    def _can_action(self, cog, interaction: discord.Interaction, panel_data: dict) -> bool:
        opener_id = _parse_opener_id(interaction.channel.topic)
        is_opener = opener_id is not None and opener_id == interaction.user.id
        is_staff = isinstance(interaction.user, discord.Member) and cog._has_panel_access(interaction.user, panel_data)
        return is_opener or is_staff

    async def confirm_callback(self, interaction: discord.Interaction):
        cog = self._tickets_cog()
        if cog is None or not cog._is_ticket_channel(interaction.channel):
            return await interaction.response.send_message("This ticket could not be found.", ephemeral=True)

        panel_data = cog._resolve_panel_data(interaction.channel)
        if not self._can_action(cog, interaction, panel_data):
            return await interaction.response.send_message(
                "You do not have permission to confirm this close request.", ephemeral=True
            )

        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                description=f"✅ Close request confirmed by {interaction.user.mention}. Closing ticket…",
                color=COLOUR_BRAND,
            ),
            view=None,
        )

        await _deliver_transcript(
            bot=cog.bot,
            guild=interaction.guild,
            channel=interaction.channel,
            panel_data=panel_data,
            reason="Closed via close request",
            closed_by=interaction.user,
        )

        db.get("ticket_state", {}).pop(str(interaction.channel.id), None)
        save_db(db)

        try:
            await interaction.channel.delete(reason=f"Close request confirmed by {interaction.user}")
        except discord.NotFound:
            pass

    async def cancel_callback(self, interaction: discord.Interaction):
        cog = self._tickets_cog()
        if cog is None or not cog._is_ticket_channel(interaction.channel):
            return await interaction.response.send_message("This ticket could not be found.", ephemeral=True)

        panel_data = cog._resolve_panel_data(interaction.channel)
        if not self._can_action(cog, interaction, panel_data):
            return await interaction.response.send_message(
                "You do not have permission to cancel this close request.", ephemeral=True
            )

        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                description=f"❌ Close request cancelled by {interaction.user.mention}.",
                color=COLOUR_BRAND,
            ),
            view=None,
        )


DEFAULT_TRANSCRIPT_CHANNEL_ID = "1531417115909685351"


def panel_data_transcript_channel_id(panel_data: dict) -> Optional[str]:
    """Resolve the transcript channel for a panel: panel-level setting first,
    then the global dashboard setting, then the hardcoded default.

    Each candidate is validated as an actual digit string before being
    accepted, so an invalid-but-truthy value at one level can't block
    falling through to the next.
    """
    for candidate in (
        panel_data.get("transcript_channel_id"),
        db.get("settings", {}).get("transcript_channel_id"),
        DEFAULT_TRANSCRIPT_CHANNEL_ID,
    ):
        if candidate and str(candidate).isdigit():
            return str(candidate)
    return None


class DynamicFormModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, panel_id: str, panel_data: dict):
        super().__init__(title=panel_data.get("name", "Form")[:45])
        self.bot = bot
        self.panel_id = panel_id
        self.panel_data = panel_data
        self.form_items = []

        questions = panel_data.get("questions", [])[:5]
        for idx, q in enumerate(questions):
            q_type = q.get("type", "text")
            label = q.get("label", f"Question {idx + 1}")[:45]
            is_req = q.get("required", True)

            if q_type in ["text", "paragraph"]:
                item = discord.ui.TextInput(
                    label=label,
                    style=discord.TextStyle.paragraph if q_type == "paragraph" else discord.TextStyle.short,
                    required=is_req,
                    custom_id=f"q_{idx}",
                )
                item._original_label = label
                self.form_items.append(item)
                self.add_item(item)

            elif q_type in ["dropdown", "multiselect"]:
                raw_opts = q.get("options", ["Option 1"])
                options = [discord.SelectOption(label=opt.strip()[:100]) for opt in raw_opts[:25]]
                if not options:
                    options = [discord.SelectOption(label="None configured")]

                max_vals = len(options) if q_type == "multiselect" else 1
                item = discord.ui.Select(
                    placeholder=label[:150],
                    min_values=1 if is_req else 0,
                    max_values=max_vals,
                    options=options,
                    custom_id=f"q_{idx}",
                )
                item._original_label = label
                self.form_items.append(item)
                self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket_channel(interaction, self.bot, self.panel_id, self.panel_data, self.form_items)


class DynamicPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot, panel_id: str, panel_data: dict):
        super().__init__(timeout=None)
        self.bot = bot
        self.panel_id = panel_id
        self.panel_data = panel_data

        btn_label = panel_data.get("button_text", "Open Ticket")
        btn_style = discord.ButtonStyle.primary
        btn_emoji = None

        payload = parse_json_field(panel_data.get("message_json")) or {}
        components = payload.get("components", [])

        if isinstance(components, list):
            for row in components:
                if row.get("type") == 1:
                    for comp in row.get("components", []):
                        if comp.get("type") == 2:
                            btn_label = comp.get("label") or btn_label
                            try:
                                btn_style = discord.ButtonStyle(comp.get("style", 1))
                            except ValueError:
                                pass
                            if "emoji" in comp:
                                e_data = comp["emoji"]
                                if e_data.get("id"):
                                    btn_emoji = discord.PartialEmoji(name=e_data.get("name"), id=int(e_data["id"]), animated=e_data.get("animated", False))
                                else:
                                    btn_emoji = e_data.get("name")
                            break
                    break

        btn = discord.ui.Button(label=btn_label, style=btn_style, emoji=btn_emoji, custom_id=f"panel_open_{panel_id}")
        btn.callback = self.panel_callback
        self.add_item(btn)

    # async def panel_callback(self, interaction: discord.Interaction):
    #     if self.panel_data.get("questions"):
    #         await interaction.response.send_modal(DynamicFormModal(self.bot, self.panel_id, self.panel_data))
    #     else:
    #         await interaction.response.defer(ephemeral=True)
    #         await create_ticket_channel(interaction, self.bot, self.panel_id, self.panel_data)
    async def panel_callback(self, interaction: discord.Interaction):
        try:
            if self.panel_data.get("questions"):
                await interaction.response.send_modal(DynamicFormModal(self.bot, self.panel_id, self.panel_data))
            else:
                await interaction.response.defer(ephemeral=True)
                await create_ticket_channel(interaction, self.bot, self.panel_id, self.panel_data)
        except Exception:
            import traceback; traceback.print_exc()


class DynamicMultiPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot, panels: dict):
        super().__init__(timeout=None)
        self.bot = bot
        self.panels = panels

        options = [discord.SelectOption(label=pdata.get("name", "Unknown Panel"), description=f"Open {pdata.get('type', 'ticket')} panel.", value=pid) for pid, pdata in panels.items()]
        if not options:
            options.append(discord.SelectOption(label="No panels configured", value="none"))

        select = discord.ui.Select(custom_id="ticket_panel_select", placeholder="Select a ticket type...", min_values=1, max_values=1, options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_pid = interaction.data["values"][0]
        if selected_pid == "none" or selected_pid not in self.panels:
            return await interaction.response.send_message("Invalid selection.", ephemeral=True)

        panel_data = self.panels[selected_pid]
        if panel_data.get("questions"):
            await interaction.response.send_modal(DynamicFormModal(self.bot, selected_pid, panel_data))
        else:
            await interaction.response.defer(ephemeral=True)
            await create_ticket_channel(interaction, self.bot, selected_pid, panel_data)


# ── Discord-styled HTML transcript (fallback when chat_exporter isn't available) ──

def _esc(text: Optional[str]) -> str:
    return html.escape(text or "")


def _render_embed_html(e: discord.Embed) -> str:
    color_hex = f"#{e.color.value:06x}" if e.color and e.color.value is not None else "#5865f2"
    parts = [f'<div class="embed" style="border-left-color:{color_hex}">', '<div class="embed-body">']

    if e.author and e.author.name:
        parts.append(f'<div class="embed-author">{_esc(e.author.name)}</div>')
    if e.title:
        parts.append(f'<div class="embed-title">{_esc(e.title)}</div>')
    if e.description:
        parts.append(f'<div class="embed-desc">{_esc(e.description).replace(chr(10), "<br>")}</div>')
    if e.fields:
        parts.append('<div class="embed-fields">')
        for f in e.fields:
            width = "embed-field-inline" if f.inline else "embed-field-full"
            parts.append(
                f'<div class="embed-field {width}"><div class="embed-field-name">{_esc(f.name)}</div>'
                f'<div class="embed-field-value">{_esc(f.value).replace(chr(10), "<br>")}</div></div>'
            )
        parts.append('</div>')
    if e.image and e.image.url:
        parts.append(f'<div class="embed-image"><img src="{e.image.url}" loading="lazy"></div>')
    if e.footer and e.footer.text:
        parts.append(f'<div class="embed-footer">{_esc(e.footer.text)}</div>')

    parts.append('</div>')  # embed-body
    if e.thumbnail and e.thumbnail.url:
        parts.append(f'<div class="embed-thumb"><img src="{e.thumbnail.url}" loading="lazy"></div>')
    parts.append('</div>')  # embed
    return "".join(parts)


async def _render_discord_style_transcript(channel: discord.abc.GuildChannel) -> str:
    """Build an HTML transcript that visually mimics Discord's dark theme
    (avatars, colored usernames, timestamps, embeds, attachments) so it's
    actually readable by staff after the fact, instead of a flat text dump.
    """
    guild = channel.guild
    messages = []
    async for m in channel.history(limit=500, oldest_first=True):
        messages.append(m)

    rows = []
    for m in messages:
        author = m.author
        avatar = getattr(author, "display_avatar", None)
        avatar_url = avatar.url if avatar else "https://cdn.discordapp.com/embed/avatars/0.png"
        name = getattr(author, "display_name", None) or str(author)

        color_hex = "#f2f3f5"
        member = guild.get_member(author.id) if guild else None
        if member and member.color and member.color.value:
            color_hex = f"#{member.color.value:06x}"

        ts = m.created_at.strftime("%m/%d/%Y %I:%M %p")

        content_html = _esc(m.content).replace("\n", "<br>")
        for user in m.mentions:
            escaped_mention = _esc(f"@{user.display_name}")
            content_html = content_html.replace(escaped_mention, f'<span class="mention">@{_esc(user.display_name)}</span>')

        embeds_html = "".join(_render_embed_html(e) for e in m.embeds)

        attach_html = ""
        for a in m.attachments:
            if a.content_type and a.content_type.startswith("image"):
                attach_html += f'<div class="attachment"><img src="{a.url}" alt="{_esc(a.filename)}" loading="lazy"></div>'
            else:
                attach_html += f'<div class="attachment"><a href="{a.url}" target="_blank">📎 {_esc(a.filename)}</a></div>'

        rows.append(f"""
        <div class="message">
            <img class="avatar" src="{avatar_url}" alt="avatar">
            <div class="msg-body">
                <div class="msg-header">
                    <span class="username" style="color:{color_hex}">{_esc(name)}</span>
                    <span class="timestamp">{ts}</span>
                </div>
                <div class="msg-content">{content_html}</div>
                {embeds_html}
                {attach_html}
            </div>
        </div>""")

    body = "\n".join(rows) if rows else '<p style="color:#949ba4;">No messages in this ticket.</p>'
    guild_name = _esc(guild.name) if guild else "Server"
    channel_name = _esc(getattr(channel, "name", "ticket"))
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Transcript - {channel_name}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:0; background:#313338; color:#dbdee1; font-family: "gg sans", "Helvetica Neue", Arial, sans-serif; }}
  .transcript-header {{ padding: 16px 24px; background:#2b2d31; border-bottom:1px solid #1e1f22; position: sticky; top: 0; }}
  .transcript-header h1 {{ margin:0; font-size:18px; color:#f2f3f5; }}
  .transcript-header p {{ margin:4px 0 0; font-size:13px; color:#949ba4; }}
  .messages {{ padding: 16px 24px 40px; max-width: 920px; margin: 0 auto; }}
  .message {{ display:flex; padding: 10px 4px; border-radius: 6px; }}
  .message:hover {{ background: #2e3035; }}
  .avatar {{ width:40px; height:40px; border-radius:50%; margin-right:16px; flex-shrink:0; }}
  .msg-body {{ flex:1; min-width:0; }}
  .msg-header {{ display:flex; align-items:baseline; gap:8px; }}
  .username {{ font-weight:600; font-size:15px; }}
  .timestamp {{ font-size:11px; color:#949ba4; }}
  .msg-content {{ font-size:15px; line-height:1.375rem; white-space:pre-wrap; word-wrap:break-word; color:#dbdee1; }}
  .mention {{ background: rgba(88,101,242,0.3); color:#c9cdfb; border-radius:3px; padding:0 2px; }}
  .embed {{ display:flex; margin-top:6px; border-left:4px solid #5865f2; background:#2b2d31; border-radius:4px; padding:10px 12px; max-width:560px; }}
  .embed-body {{ flex:1; min-width:0; }}
  .embed-author {{ font-weight:600; font-size:13px; color:#f2f3f5; margin-bottom:4px; }}
  .embed-title {{ font-weight:600; font-size:15px; color:#f2f3f5; margin-bottom:4px; }}
  .embed-desc {{ font-size:14px; color:#dbdee1; white-space:pre-wrap; }}
  .embed-fields {{ display:flex; flex-wrap:wrap; gap:8px 16px; margin-top:8px; }}
  .embed-field-inline {{ flex: 1 1 30%; min-width:120px; }}
  .embed-field-full {{ flex: 1 1 100%; }}
  .embed-field-name {{ font-weight:600; font-size:13px; color:#f2f3f5; }}
  .embed-field-value {{ font-size:13px; color:#dbdee1; }}
  .embed-image img {{ border-radius:4px; margin-top:8px; max-width:100%; display:block; }}
  .embed-thumb {{ margin-left:12px; flex-shrink:0; }}
  .embed-thumb img {{ width:80px; height:80px; object-fit:cover; border-radius:4px; }}
  .embed-footer {{ font-size:12px; color:#949ba4; margin-top:8px; }}
  .attachment img {{ max-width:400px; border-radius:4px; margin-top:6px; display:block; }}
  .attachment a {{ color:#00a8fc; text-decoration:none; }}
</style>
</head>
<body>
  <div class="transcript-header">
    <h1># {channel_name}</h1>
    <p>{guild_name} • {len(messages)} messages • Exported {exported_at}</p>
  </div>
  <div class="messages">
    {body}
  </div>
</body>
</html>"""


async def export_transcript(channel: discord.abc.GuildChannel) -> str:
    if chat_exporter is not None:
        try:
            transcript = await chat_exporter.export(channel)
            if transcript:
                return transcript
        except Exception as exc:
            print(f"[TRANSCRIPT ERROR] chat_exporter failed, falling back to styled HTML export: {exc}")

    return await _render_discord_style_transcript(channel)


def _replace_user_placeholder(embed: discord.Embed, mention: str) -> discord.Embed:
    """Replace {user} anywhere inside an embed (title, description, footer,
    author, and every field) with an actual mention, not just in the
    top-level message content."""
    if embed.title:
        embed.title = embed.title.replace("{user}", mention)
    if embed.description:
        embed.description = embed.description.replace("{user}", mention)
    if embed.footer and embed.footer.text:
        embed.set_footer(text=embed.footer.text.replace("{user}", mention), icon_url=embed.footer.icon_url)
    if embed.author and embed.author.name:
        embed.set_author(name=embed.author.name.replace("{user}", mention), url=embed.author.url, icon_url=embed.author.icon_url)
    if embed.fields:
        fields = [(f.name, f.value, f.inline) for f in embed.fields]
        embed.clear_fields()
        for name, value, inline in fields:
            new_name = name.replace("{user}", mention) if name else name
            new_value = value.replace("{user}", mention) if value else value
            embed.add_field(name=new_name, value=new_value, inline=inline)
    return embed


async def _send_welcome_message(
    channel: discord.TextChannel,
    content: Optional[str],
    embeds: list,
    view: discord.ui.View,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> discord.Message:
    """Sends the ticket's welcome message. If a template specifies a custom
    `username`/`avatar_url` (e.g. the Report Ticket branding), this sends it
    through a one-off channel webhook — regular bot messages can't override
    the sender identity — then deletes the webhook immediately afterward
    since it's only needed for this single send. Falls back to a normal bot
    message if webhook creation fails for any reason (missing permission,
    webhook cap hit, etc.) so ticket creation never hard-fails over branding.
    """
    if username or avatar_url:
        try:
            webhook = await channel.create_webhook(
                name=(username or "Ticket")[:80], reason="One-time branded ticket welcome message"
            )
            try:
                return await webhook.send(
                    content=content or None,
                    embeds=embeds[:10] if embeds else [],
                    username=username or None,
                    avatar_url=avatar_url or None,
                    view=view,
                    wait=True,
                )
            finally:
                await webhook.delete(reason="Branded welcome message already sent")
        except Exception as exc:
            print(f"[WEBHOOK WELCOME ERROR] Falling back to normal send: {exc}")

    return await channel.send(content=content or None, embeds=embeds[:10] if embeds else None, view=view)


async def _create_ticket_channel_core(
    interaction: discord.Interaction,
    bot: commands.Bot,
    panel_id: str,
    panel_data: dict,
    content: Optional[str],
    embeds: list,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> discord.TextChannel:
    """Shared low-level ticket-channel builder: creates the channel with the
    right category/overwrites, sends the (already-built) welcome message +
    claim/close control view, and kicks off the support-discussion thread.
    Does NOT touch the interaction response — callers decide how to notify
    the user (a followup for the classic modal flow, an edited ephemeral
    message for the AI-triage flow, etc.), since those two flows are at
    different points in the interaction lifecycle when this runs.
    """
    guild = interaction.guild
    category_id = panel_data.get("category_id")
    category = guild.get_channel(int(category_id)) if category_id and str(category_id).isdigit() else None

    format_str = panel_data.get("channel_name_format", "")
    if not format_str:
        format_str = f"{panel_data.get('name', 'ticket')}-{{user}}"

    existing_names = {ch.name for ch in guild.text_channels}
    channel_name = _build_channel_name(format_str, interaction.user.name, existing_names)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True, manage_threads=True, create_private_threads=True)
    }

    role_id = panel_data.get("role_id")
    if role_id and str(role_id).isdigit():
        role = guild.get_role(int(role_id))
        if role:
            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        topic=f"User ID: {interaction.user.id} | Panel ID: {panel_id} | Panel: {panel_data.get('name')}",
        overwrites=overwrites
    )

    control_view = TicketControlView(bot, panel_id, panel_data)
    await _send_welcome_message(channel, content, embeds, control_view, username=username, avatar_url=avatar_url)

    # The support-team discussion thread is created after the ticket channel
    # + welcome message have already been sent, so thread creation never
    # blocks the caller's confirmation to the user.
    asyncio.create_task(_create_discussion_thread(channel, role_id))

    return channel


async def create_ticket_channel(interaction: discord.Interaction, bot: commands.Bot, panel_id: str, panel_data: dict, modal_items=None):
    guild = interaction.guild

    # --- WELCOME MESSAGE PARSER (JSON vs Plain Text) ---
    raw_welcome = panel_data.get("welcome_message", "")
    welcome_payload = parse_json_field(raw_welcome)

    embeds: list = []
    content_msg = ""

    if welcome_payload and isinstance(welcome_payload, dict):
        content_msg = welcome_payload.get("content", "") or ""
        embeds_list = welcome_payload.get("embeds", [])
        if embeds_list and isinstance(embeds_list, list):
            for e_data in embeds_list[:10]:
                try:
                    embeds.append(discord.Embed.from_dict(e_data))
                except Exception as exc:
                    print(f"[WELCOME EMBED ERROR] Failed to parse embed: {exc}")
        elif "title" in welcome_payload or "description" in welcome_payload:
            try:
                embeds.append(discord.Embed.from_dict(welcome_payload))
            except Exception as exc:
                print(f"[WELCOME EMBED ERROR] Failed to parse embed: {exc}")
    else:
        # Plain text mode! Do not force an empty embed or banner.
        content_msg = raw_welcome

    if modal_items:
        answers_embed = discord.Embed(color=get_guild_color(guild.id))
        for item in modal_items:
            if isinstance(item, discord.ui.TextInput):
                field_name = getattr(item, "_original_label", "Question")
                answers_embed.add_field(name=field_name, value=item.value or "", inline=False)
            elif isinstance(item, discord.ui.Select):
                val = ", ".join(item.values) if item.values else "No selection made"
                answers_embed.add_field(name=getattr(item, "_original_label", "Selection"), value=val, inline=False)
        embeds.append(answers_embed)

    content_msg = content_msg.replace("{user}", interaction.user.mention)

    for e in embeds:
        _replace_user_placeholder(e, interaction.user.mention)

    channel = await _create_ticket_channel_core(interaction, bot, panel_id, panel_data, content_msg, embeds)
    await interaction.followup.send(f"Created at {channel.mention}", ephemeral=True)


async def _create_discussion_thread(channel: discord.TextChannel, role_id: Optional[str]):
    """Create the internal "Support Team Discussion" thread off the bare
    channel (not off the welcome message). Discord auto-posts a system
    message ("X started a thread: ...") in the parent channel whenever a
    thread is created this way — we immediately delete that notice so
    nothing shows up attached to the welcome message or floating in the
    channel. Creating it off the welcome message instead (a previous
    approach) avoided that system message but replaced it with a visible
    "N replies" thread indicator stuck under the welcome message itself,
    which looks worse, so this reverts to channel-based creation.
    """
    try:
        thread = await channel.create_thread(
            name="Support Team Discussion",
            auto_archive_duration=10080,
            type=discord.ChannelType.public_thread,
            reason="Ticket discussion thread",
        )

        async for m in channel.history(limit=5):
            if m.type == discord.MessageType.thread_created:
                try:
                    await m.delete()
                except discord.NotFound:
                    pass
                break

        # The thread's top message is just the (fake, plain-text) role
        # ping — the original ticket-opening message is never copied in.
        msg = await thread.send(".")
        if role_id and str(role_id).isdigit():
            await msg.edit(content=f"<@&{role_id}>")
        else:
            await msg.edit(content="Support team discussion started.")
    except Exception as exc:
        print(f"[THREAD ERROR] Failed to create support discussion thread: {exc}")


# ── Select-menu triage system: modals, AI-assisted views ────────────────

async def _open_triage_ticket(
    interaction: discord.Interaction,
    bot: commands.Bot,
    panel_key: str,
    template: dict,
    field_values: list,
    *,
    edit_target: Optional[str] = None,
) -> discord.TextChannel:
    """Builds a hardcoded welcome payload from `template`, opens the ticket
    channel, and reports the result back to the user. If `edit_target` is
    given, the result is reported by editing that ephemeral message
    (the AI-triage "Need more help" path); otherwise it's a normal
    ephemeral followup (the direct modal-submit paths).
    """
    panel_data = TRIAGE_PANELS[panel_key]
    payload = _build_triage_welcome(template, interaction.user.mention, field_values)
    embeds = [discord.Embed.from_dict(e) for e in payload.get("embeds", [])]
    content = payload.get("content") or None

    channel = await _create_ticket_channel_core(
        interaction, bot, panel_key, panel_data, content, embeds,
        username=payload.get("username"), avatar_url=payload.get("avatar_url"),
    )

    confirmation = f"✅ Ticket created at {channel.mention}"
    if edit_target == "edit_original":
        await interaction.edit_original_response(content=confirmation, embed=None, view=None)
    else:
        await interaction.followup.send(confirmation, ephemeral=True)
    return channel


class TriageResponseView(discord.ui.View):
    """The two gray escape-hatch buttons shown under a tentative AI answer
    on a General Inquiry submission. Ephemeral + single-use, so this is a
    plain (non-persistent) view scoped to one interaction rather than
    something registered with `bot.add_view`."""

    def __init__(self, bot: commands.Bot, inquiry_text: str):
        super().__init__(timeout=600)
        self.bot = bot
        self.inquiry_text = inquiry_text

        thats_all_btn = discord.ui.Button(label="That's all!", style=discord.ButtonStyle.secondary)
        thats_all_btn.callback = self.thats_all_callback
        need_help_btn = discord.ui.Button(label="Need more help", style=discord.ButtonStyle.secondary)
        need_help_btn.callback = self.need_help_callback

        self.add_item(thats_all_btn)
        self.add_item(need_help_btn)

    async def thats_all_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(description="Thanks for reaching out to our support!", color=COLOUR_BRAND),
            view=None,
        )
        self.stop()

    async def need_help_callback(self, interaction: discord.Interaction):
        # Acknowledge immediately by stripping the buttons — channel
        # creation can take a beat and we don't want the buttons clickable
        # twice in the meantime.
        await interaction.response.edit_message(
            embed=discord.Embed(description="Opening a ticket for you…", color=COLOUR_BRAND),
            view=None,
        )
        try:
            await _open_triage_ticket(
                interaction, self.bot, "general_inquiry", GENERAL_INQUIRY_WELCOME_TEMPLATE,
                [self.inquiry_text], edit_target="edit_original",
            )
        except Exception as exc:
            print(f"[TRIAGE ERROR] need_help ticket creation failed: {exc}")
            try:
                await interaction.edit_original_response(
                    content="Something went wrong opening your ticket. Please try again.", embed=None, view=None,
                )
            except Exception:
                pass
        self.stop()


class GeneralInquiryModal(discord.ui.Modal, title="General Inquiry"):
    inquiry = discord.ui.TextInput(
        label="Please state your inquiry:",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        inquiry_text = str(self.inquiry.value)

        manager = _get_autoreply_manager(self.bot)
        best = None
        if manager:
            try:
                candidates = await manager.find_matches(inquiry_text)
                if candidates:
                    best = candidates[0]
            except Exception as exc:
                print(f"[TRIAGE AI ERROR] find_matches failed: {exc}")

        if best and best[2] >= TRIAGE_MATCH_FLOOR:
            _, response_text, score = best
            embed = discord.Embed(description=response_text, color=COLOUR_BRAND)
            if score >= TRIAGE_CONFIDENT_THRESHOLD:
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                view = TriageResponseView(self.bot, inquiry_text)
                msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
                view.message = msg
            return

        # No confident AI match at all — go straight to opening the ticket.
        await _open_triage_ticket(
            interaction, self.bot, "general_inquiry", GENERAL_INQUIRY_WELCOME_TEMPLATE, [inquiry_text],
        )


class ReportTicketModal(discord.ui.Modal, title="Report Ticket"):
    staff_member = discord.ui.TextInput(label="Reported Staff Member", style=discord.TextStyle.short, required=True, max_length=100)
    description = discord.ui.TextInput(label="Brief Description", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    proof = discord.ui.TextInput(label="Proof?", style=discord.TextStyle.short, required=True, max_length=200)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        manager = _get_autoreply_manager(self.bot)
        accepted = await _assess_report_proof(manager, str(self.proof.value))

        if not accepted:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=(
                        "❌ Your report has been voided because no proof was provided. Reports without "
                        "clear evidence (screenshots, video, etc.) cannot be investigated by our IA team. "
                        "You're welcome to re-submit once you have proof available."
                    ),
                    color=COLOUR_ERROR,
                ),
                ephemeral=True,
            )
            return

        await _open_triage_ticket(
            interaction, self.bot, "report_ticket", REPORT_TICKET_WELCOME_TEMPLATE,
            [str(self.staff_member.value), str(self.description.value), str(self.proof.value)],
        )


class PurchaseTicketModal(discord.ui.Modal, title="Purchase Ticket"):
    inquiry = discord.ui.TextInput(
        label="Please state your inquiry:",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _open_triage_ticket(
            interaction, self.bot, "purchase_ticket", PURCHASE_TICKET_WELCOME_TEMPLATE, [str(self.inquiry.value)],
        )


class FAQLayoutView(discord.ui.LayoutView):
    """Ephemeral, read-only — no custom_ids needed since it holds no
    interactive components, so this doesn't need persistent registration."""

    def __init__(self):
        super().__init__(timeout=300)
        container = discord.ui.Container(accent_colour=15239956)
        section = discord.ui.Section(
            discord.ui.TextDisplay(FAQ_TEXT),
            accessory=discord.ui.Thumbnail(media="https://i.imgur.com/9KCE36G.png"),
        )
        container.add_item(section)
        self.add_item(container)


class AssistanceLayoutView(discord.ui.LayoutView):
    """The persistent Components V2 panel deployed to the assistance
    channel: banner, FAQ button, ticket-type select menu, banner. Registered
    once via `bot.add_view` in `Tickets.cog_load` so it keeps responding
    after restarts, same as every other persistent view in this cog.
    """

    SELECT_VALUE_TO_PANEL = {
        "LNsf2mwMpx": "general_inquiry",
        "YpSy6QA7Tx": "report_ticket",
        "xZeI8Gr65M": "purchase_ticket",
    }

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

        top_banner = discord.ui.Container(accent_colour=15437332)
        top_banner.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media="https://i.imgur.com/dhveVDN.png"))
        )
        self.add_item(top_banner)

        main = discord.ui.Container(accent_colour=15437332, spoiler=False)

        faq_btn = discord.ui.Button(style=discord.ButtonStyle.secondary, label="FAQs", custom_id="p_332643082929442817")
        faq_btn.callback = self.faq_callback
        main.add_item(discord.ui.Section(discord.ui.TextDisplay(ASSISTANCE_INTRO_TEXT), accessory=faq_btn))

        main.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        main.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(ASSISTANCE_OPTIONS_TEXT),
                accessory=discord.ui.Thumbnail(media="https://i.imgur.com/9KCE36G.png"),
            )
        )

        main.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        main.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media="https://i.imgur.com/y13i2M7.png"))
        )
        self.add_item(main)

        select = discord.ui.Select(
            custom_id="p_332645748661293061",
            placeholder="Select a support option...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="General Inquiries",
                    value="LNsf2mwMpx",
                    description="Any questions/comments/concerns that are not staff reports.",
                    emoji=discord.PartialEmoji(name="notes", id=1531825297798725682),
                ),
                discord.SelectOption(
                    label="Report Ticket",
                    value="YpSy6QA7Tx",
                    description="Reports on staff members for their conduct with solid, undeniable evidence",
                    emoji=discord.PartialEmoji(name="gavel", id=1531869255476314253),
                ),
                discord.SelectOption(
                    label="Purchase Redemption",
                    value="xZeI8Gr65M",
                    description="All transaction-related support, including questions, redemptions, etc.",
                    emoji=discord.PartialEmoji(name="card", id=1535048863281451129),
                ),
            ],
        )
        select.callback = self.select_callback
        action_row = discord.ui.ActionRow(select)
        self.add_item(action_row)

    async def faq_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=FAQLayoutView(), ephemeral=True)

    async def select_callback(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        panel_key = self.SELECT_VALUE_TO_PANEL.get(value)

        if panel_key == "general_inquiry":
            await interaction.response.send_modal(GeneralInquiryModal(self.bot))
        elif panel_key == "report_ticket":
            await interaction.response.send_modal(ReportTicketModal(self.bot))
        elif panel_key == "purchase_ticket":
            await interaction.response.send_modal(PurchaseTicketModal(self.bot))
        else:
            await interaction.response.send_message("Invalid selection.", ephemeral=True)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.panels = dict(DEFAULT_PANELS)
        self.web_server_started = False
        self.web_runner = None

        if db.get("panels"):
            self.panels.update({pid: data for pid, data in db["panels"].items() if isinstance(data, dict)})
        self.multi_panels = dict(db.get("multi_panels") or {})

    async def cog_load(self):
        # Re-register persistent views for every saved panel / multi-panel on
        # startup. Without this, buttons stop responding ("This interaction
        # failed") after a bot restart until the panel is redeployed.
        self.bot.add_view(TicketControlView(self.bot, "generic", {}))
        self.bot.add_view(CloseRequestView(self.bot))
        self.bot.add_view(AssistanceLayoutView(self.bot))
        for pid, pdata in self.panels.items():
            try:
                self.bot.add_view(DynamicPanelView(self.bot, pid, pdata))
                self.bot.add_view(TicketControlView(self.bot, pid, pdata))
            except Exception as exc:
                print(f"[VIEW REGISTER ERROR] panel {pid}: {exc}")
        for mpid, mdata in self.multi_panels.items():
            try:
                linked_panels = {pid: self.panels[pid] for pid in mdata.get("panels", {}) if pid in self.panels}
                self.bot.add_view(DynamicMultiPanelView(self.bot, linked_panels))
            except Exception as exc:
                print(f"[VIEW REGISTER ERROR] multi-panel {mpid}: {exc}")

        if os.environ.get("ENABLE_WEB_DASHBOARD", "1") == "1":
            asyncio.create_task(self.start_web_server())

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        # Every command in this cog is a hybrid command. When one is
        # invoked via the text prefix (not a slash command), delete the
        # invoking message so it doesn't clutter the ticket channel.
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

    def _is_ticket_channel(self, channel: discord.abc.GuildChannel) -> bool:
        return isinstance(channel, discord.TextChannel) and bool(channel.topic and "User ID:" in channel.topic and "Panel:" in channel.topic)

    def _has_panel_access(self, member: discord.Member, panel_data: dict) -> bool:
        role_id = panel_data.get("role_id")
        if role_id and str(role_id).isdigit():
            role = member.guild.get_role(int(role_id))
            if role and role not in member.roles and not member.guild_permissions.administrator:
                return False
        return True

    def _find_panel_by_name_or_id(self, query: str):
        query = (query or "").strip()
        if not query:
            return None, None
        panels = self._all_panels()
        if query in panels:
            return query, panels[query]
        q_lower = query.lower()
        for pid, pdata in panels.items():
            if pdata.get("name", "").lower() == q_lower:
                return pid, pdata
        for pid, pdata in panels.items():
            if q_lower in pdata.get("name", "").lower():
                return pid, pdata
        return None, None
    
    def _all_panels(self) -> dict:
        """Dashboard-configured panels + the three hardcoded triage panels
        (general_inquiry/report_ticket/purchase_ticket), merged so commands
        like /escalate and ticket-channel resolution can see both kinds.
        TRIAGE_PANELS entries have the same shape (name/category_id/role_id/
        channel_name_format) that escalate and _resolve_panel_data need."""
        combined = dict(TRIAGE_PANELS)
        combined.update(self.panels)
        return combined

    @commands.hybrid_command(
        name="ticketcolor",
        description="Set this server's embed color for ticket form answers.",
    )
    @app_commands.describe(color="Hex color, e.g. #5865F2 (leave blank to reset to the default)")
    async def ticket_color_cmd(self, ctx: commands.Context, *, color: Optional[str] = None):
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.", ephemeral=True)
            return
        if not (ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator):
            await ctx.send("You need the **Manage Server** permission to do this.", ephemeral=True)
            return

        if not color or not color.strip():
            db.get("settings", {}).get("guild_colors", {}).pop(str(ctx.guild.id), None)
            save_db(db)
            await ctx.send(
                embed=discord.Embed(
                    description="✅ Reset this server's ticket form-answer embed color to the default.",
                    color=COLOUR_BRAND,
                ),
                ephemeral=True,
            )
            return

        raw = color.strip().lstrip("#")
        try:
            value = int(raw, 16)
            if not (0 <= value <= 0xFFFFFF):
                raise ValueError
        except ValueError:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ That's not a valid hex color. Try something like `#5865F2`.",
                    color=COLOUR_ERROR,
                ),
                ephemeral=True,
            )
            return

        set_guild_color(ctx.guild.id, value)
        await ctx.send(
            embed=discord.Embed(
                description=f"✅ Ticket form-answer embeds in this server will now use `#{value:06X}`.",
                color=value,
            ),
            ephemeral=True,
        )

    @commands.hybrid_command(
        name="deployassistance",
        description="Post the assistance channel panel (select menu + FAQs) here or in a target channel.",
    )
    @app_commands.describe(channel="Where to post the panel (defaults to this channel)")
    async def deploy_assistance_cmd(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.", ephemeral=True)
            return
        if not (ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator):
            await ctx.send("You need the **Manage Server** permission to do this.", ephemeral=True)
            return

        target = channel or ctx.channel
        await target.send(view=AssistanceLayoutView(self.bot))
        await ctx.send(f"✅ Assistance panel posted in {target.mention}.", ephemeral=True)

    @commands.hybrid_command(name="ticket", description="Open a support ticket")
    async def ticket(self, ctx: commands.Context):
        try:
            if not ctx.guild:
                await ctx.send("This command can only be used in a server.")
                return
            if len(self.panels) == 1:
                panel_id = next(iter(self.panels))
                panel_data = self.panels[panel_id]
                if panel_data.get("questions"):
                    if ctx.interaction is not None:
                        await ctx.defer(ephemeral=True)
                        await ctx.interaction.response.send_modal(DynamicFormModal(self.bot, panel_id, panel_data))
                    else:
                        await ctx.send("Ticket forms are only available through slash commands.")
                else:
                    if ctx.interaction is not None:
                        await ctx.defer(ephemeral=True)
                        await create_ticket_channel(ctx.interaction, self.bot, panel_id, panel_data)
                    else:
                        await ctx.send("Ticket creation is only available through slash commands.")
                return
        except Exception:
            import traceback; traceback.print_exc()


        view = DynamicMultiPanelView(self.bot, self.panels)
        if ctx.interaction is not None:
            await ctx.send("Select a ticket type below.", view=view, ephemeral=True)
        else:
            await ctx.send("Select a ticket type below.", view=view)

    def _resolve_panel_data(self, channel: discord.TextChannel) -> dict:
        pid = _parse_panel_id(channel.topic)
        panels = self._all_panels()
        if pid and pid in panels:
            return panels[pid]

        for _pid, pdata in panels.items():
            if pdata.get("name", "").lower() and pdata.get("name", "").lower() in channel.name.lower():
                return pdata
        return {}

    @commands.hybrid_command(name="close", description="Close the current ticket")
    async def close_cmd(self, ctx: commands.Context, *, reason: Optional[str] = "No reason provided"):
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        panel_data = self._resolve_panel_data(ctx.channel)

        await _deliver_transcript(
            bot=self.bot,
            guild=ctx.guild,
            channel=ctx.channel,
            panel_data=panel_data,
            reason=reason,
            closed_by=ctx.author,
        )

        db.get("ticket_state", {}).pop(str(ctx.channel.id), None)
        save_db(db)

        try:
            await ctx.channel.delete(reason=f"Closed by {ctx.author}: {reason}")
        except discord.NotFound:
            pass

    @commands.hybrid_command(name="delete", description="Delete the current ticket without saving a transcript.")
    async def delete_cmd(self, ctx: commands.Context, *, reason: Optional[str] = "No reason provided"):
        # `-delete` is its own standalone flow (not an alias for `-close`),
        # so it never exports or sends a transcript at all.
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        db.get("ticket_state", {}).pop(str(ctx.channel.id), None)
        save_db(db)

        try:
            await ctx.channel.delete(reason=f"Deleted (no transcript) by {ctx.author}: {reason}")
        except discord.NotFound:
            pass

    async def _escalate_panel_autocomplete(self, interaction: discord.Interaction, current: str):
        current = (current or "").lower()
        choices = []
        for pid, pdata in self._all_panels().items():
            name = pdata.get("name", pid)
            if current in name.lower():
                choices.append(app_commands.Choice(name=name[:100], value=name))
        return choices[:25]

    @commands.hybrid_command(name="escalate", description="Escalate this ticket to a different team/panel.")
    @app_commands.describe(panel="The team/panel to escalate this ticket to")
    @app_commands.autocomplete(panel=_escalate_panel_autocomplete)
    async def escalate_cmd(self, ctx: commands.Context, *, panel: str):
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.")
            return
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        current_panel_data = self._resolve_panel_data(ctx.channel)
        if isinstance(ctx.author, discord.Member) and not self._has_panel_access(ctx.author, current_panel_data):
            await ctx.send("You do not have permission to escalate this ticket.", ephemeral=True)
            return

        target_pid, target_data = self._find_panel_by_name_or_id(panel)
        if not target_data:
            available = ", ".join(p.get("name", pid) for pid, p in self.panels.items()) or "None configured"
            await ctx.send(f"Couldn't find a panel matching `{panel}`. Available panels: {available}", ephemeral=True)
            return

        guild = ctx.guild
        channel = ctx.channel
        opener_id = _parse_opener_id(channel.topic)
        opener = guild.get_member(opener_id) if opener_id else None

        # Rebuild permission overwrites for the new panel: opener + bot stay,
        # the old panel's role is dropped, the new panel's role is granted.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True, manage_threads=True, create_private_threads=True),
        }
        if opener:
            overwrites[opener] = discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)
        elif opener_id:
            overwrites[discord.Object(id=opener_id)] = discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)

        new_role_id = target_data.get("role_id")
        if new_role_id and str(new_role_id).isdigit():
            new_role = guild.get_role(int(new_role_id))
            if new_role:
                overwrites[new_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # Rebuild the channel name using the target panel's naming format.
        format_str = target_data.get("channel_name_format") or f"{target_data.get('name', 'ticket')}-{{user}}"
        member_name = opener.name if opener else (str(opener_id) if opener_id else "user")
        existing_names = {ch.name for ch in guild.text_channels if ch.id != channel.id}
        new_name = _build_channel_name(format_str, member_name, existing_names)

        new_category = None
        cat_id = target_data.get("category_id")
        if cat_id and str(cat_id).isdigit():
            new_category = guild.get_channel(int(cat_id))

        new_topic = f"User ID: {opener_id or ''} | Panel ID: {target_pid} | Panel: {target_data.get('name')}"

        try:
            await channel.edit(
                name=new_name,
                category=new_category,
                topic=new_topic,
                overwrites=overwrites,
                reason=f"Escalated to {target_data.get('name')} by {ctx.author}",
            )
        except Exception as exc:
            await ctx.send(f"Failed to escalate this ticket: {exc}", ephemeral=True)
            return

        embeds = []
        for e_data in ESCALATION_MESSAGE_PAYLOAD.get("embeds", []):
            try:
                embeds.append(discord.Embed.from_dict(e_data))
            except Exception:
                pass

        # Ping the ticket owner and the newly-assigned support role alongside
        # the escalation embed. No control view is attached here anymore —
        # the original claim/close buttons already live on the channel's
        # welcome message, so this notice doesn't need its own copy of them.
        mentions = []
        if opener:
            mentions.append(opener.mention)
        elif opener_id:
            mentions.append(f"<@{opener_id}>")

        new_role = guild.get_role(int(new_role_id)) if new_role_id and str(new_role_id).isdigit() else None
        if new_role:
            mentions.append(new_role.mention)

        escalation_content = " ".join(mentions) if mentions else None

        await channel.send(
            content=escalation_content,
            embeds=embeds,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )

        await ctx.send(f"✅ Ticket escalated to **{target_data.get('name')}**.", ephemeral=True)

    async def _sync_claim_button(self, channel: discord.TextChannel, claimed: bool) -> None:
        """Best-effort: find the ticket's welcome message (the first bot
        message carrying the Claim/Close buttons) and update its Claim
        button label so /claim and /unclaim stay visually in sync with the
        button itself, not just the other way around.
        """
        panel_data = self._resolve_panel_data(channel)
        try:
            async for m in channel.history(limit=50, oldest_first=True):
                if m.author.id != self.bot.user.id or not m.components:
                    continue

                claim_custom_id = None
                for row in m.components:
                    for comp in getattr(row, "children", []):
                        cid = getattr(comp, "custom_id", None)
                        if cid and cid.startswith("claim_btn_"):
                            claim_custom_id = cid
                            break
                    if claim_custom_id:
                        break

                if not claim_custom_id:
                    continue

                panel_id = claim_custom_id[len("claim_btn_"):]
                new_view = TicketControlView(self.bot, panel_id, panel_data)
                claim_button = discord.utils.get(new_view.children, custom_id=claim_custom_id)
                if claim_button:
                    claim_button.label = "Unclaim" if claimed else "Claim"
                await m.edit(view=new_view)
                return
        except Exception as exc:
            print(f"[CLAIM SYNC ERROR] {exc}")

    @commands.hybrid_command(name="claim", description="Claim the current ticket.")
    async def claim_cmd(self, ctx: commands.Context):
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        panel_data = self._resolve_panel_data(ctx.channel)
        if isinstance(ctx.author, discord.Member) and not self._has_panel_access(ctx.author, panel_data):
            await ctx.send("You do not have permission to claim this ticket.", ephemeral=True)
            return

        state = db.setdefault("ticket_state", {}).setdefault(str(ctx.channel.id), {})
        claimed_by = state.get("claimed_by")
        if claimed_by:
            claimer = ctx.guild.get_member(claimed_by)
            embed = discord.Embed(
                description=f"❌ This ticket is already claimed by {claimer.mention if claimer else 'someone'}.",
                color=COLOUR_ERROR,
            )
            await ctx.send(embed=embed, ephemeral=True)
            return

        state["claimed_by"] = ctx.author.id
        save_db(db)
        await self._sync_claim_button(ctx.channel, claimed=True)

        embed = discord.Embed(description=f"🔒 {ctx.author.mention} has claimed this ticket.", color=COLOUR_BRAND)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    @commands.hybrid_command(name="unclaim", description="Unclaim the current ticket.")
    async def unclaim_cmd(self, ctx: commands.Context):
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        state = db.setdefault("ticket_state", {}).setdefault(str(ctx.channel.id), {})
        claimed_by = state.get("claimed_by")
        if not claimed_by:
            await ctx.send("This ticket isn't currently claimed.", ephemeral=True)
            return

        is_admin = isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator
        if claimed_by != ctx.author.id and not is_admin:
            await ctx.send(
                "Only the staff member who claimed this ticket (or an admin) can unclaim it.",
                ephemeral=True,
            )
            return

        state["claimed_by"] = None
        save_db(db)
        await self._sync_claim_button(ctx.channel, claimed=False)

        embed = discord.Embed(description=f"🔓 {ctx.author.mention} has unclaimed this ticket.", color=COLOUR_BRAND)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    @commands.hybrid_command(name="rename", description="Rename the current ticket channel.")
    @app_commands.describe(name="The new name for this ticket channel")
    async def rename_cmd(self, ctx: commands.Context, *, name: str):
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        panel_data = self._resolve_panel_data(ctx.channel)
        if isinstance(ctx.author, discord.Member) and not self._has_panel_access(ctx.author, panel_data):
            await ctx.send("You do not have permission to rename this ticket.", ephemeral=True)
            return

        existing_names = {ch.name for ch in ctx.guild.text_channels if ch.id != ctx.channel.id}
        new_name = name.strip().lower().replace(" ", "-")

        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        try:
            await ctx.channel.edit(name=new_name, reason=f"Renamed by {ctx.author}")
        except Exception as exc:
            await ctx.send(f"Failed to rename this ticket: {exc}", ephemeral=True)
            return

    @commands.hybrid_command(name="add", description="Add a member to this ticket.")
    @app_commands.describe(member="The member to add to this ticket")
    async def add_cmd(self, ctx: commands.Context, member: discord.Member):
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        panel_data = self._resolve_panel_data(ctx.channel)
        if isinstance(ctx.author, discord.Member) and not self._has_panel_access(ctx.author, panel_data):
            await ctx.send("You do not have permission to add members to this ticket.", ephemeral=True)
            return

        try:
            await ctx.channel.set_permissions(
                member,
                read_messages=True,
                send_messages=True,
                attach_files=True,
                reason=f"Added by {ctx.author}",
            )
        except Exception as exc:
            await ctx.send(f"Failed to add {member.mention}: {exc}", ephemeral=True)
            return

        embed = discord.Embed(
            description=f"➕ {member.mention} has been added to this ticket by {ctx.author.mention}.",
            color=COLOUR_BRAND,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    @commands.hybrid_command(name="remove", description="Remove a member from this ticket.")
    @app_commands.describe(member="The member to remove from this ticket")
    async def remove_cmd(self, ctx: commands.Context, member: discord.Member):
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        panel_data = self._resolve_panel_data(ctx.channel)
        if isinstance(ctx.author, discord.Member) and not self._has_panel_access(ctx.author, panel_data):
            await ctx.send("You do not have permission to remove members from this ticket.", ephemeral=True)
            return

        opener_id = _parse_opener_id(ctx.channel.topic)
        if opener_id and member.id == opener_id:
            await ctx.send("You can't remove the ticket opener. Use `/close` instead.", ephemeral=True)
            return

        try:
            await ctx.channel.set_permissions(member, overwrite=None, reason=f"Removed by {ctx.author}")
        except Exception as exc:
            await ctx.send(f"Failed to remove {member.mention}: {exc}", ephemeral=True)
            return

        embed = discord.Embed(
            description=f"➖ {member.mention} has been removed from this ticket by {ctx.author.mention}.",
            color=COLOUR_BRAND,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    @commands.hybrid_command(name="closerequest", description="Ask for confirmation before closing this ticket.")
    async def closerequest_cmd(self, ctx: commands.Context, *, reason: Optional[str] = "Resolved"):
        if not self._is_ticket_channel(ctx.channel):
            await ctx.send("This command can only be used in a ticket channel.", ephemeral=True)
            return

        panel_data = self._resolve_panel_data(ctx.channel)
        if isinstance(ctx.author, discord.Member) and not self._has_panel_access(ctx.author, panel_data):
            await ctx.send("You do not have permission to request closing this ticket.", ephemeral=True)
            return

        opener_id = _parse_opener_id(ctx.channel.topic)
        opener = ctx.guild.get_member(opener_id) if opener_id else None

        embed = discord.Embed(
            title="Close Request",
            description=(
                f"{ctx.author.mention} has requested to close this ticket.\n"
                f"**Reason:** {reason}\n\n"
                "Please confirm below to close the ticket, or cancel to keep it open."
            ),
            color=COLOUR_BRAND,
        )

        content = opener.mention if opener else (f"<@{opener_id}>" if opener_id else None)

        await ctx.send(
            content=content,
            embed=embed,
            view=CloseRequestView(self.bot),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @web.middleware
    async def auth_middleware(self, request, handler):
        auth_header = request.headers.get("Authorization")
        expected_auth = f"Basic {base64.b64encode(b'admin:' + WEB_SECRET.encode()).decode()}"
        if auth_header != expected_auth:
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Dashboard"'}, text="Unauthorized")
        return await handler(request)

    async def start_web_server(self):
        if self.web_server_started:
            return
        app = web.Application(middlewares=[self.auth_middleware])
        app.router.add_get("/", self.route_app)
        app.router.add_post("/api/save_panel", self.api_save_panel)
        app.router.add_post("/api/delete_panel", self.api_delete_panel)
        app.router.add_post("/api/save_multi_panel", self.api_save_multi_panel)
        app.router.add_post("/api/delete_multi_panel", self.api_delete_multi_panel)
        app.router.add_post("/api/deploy", self.api_deploy)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        self.web_server_started = True
        self.web_runner = runner
        print(f"[TICKETS] Web dashboard started on 0.0.0.0:{PORT}")

    async def route_app(self, request):
        db_json = json.dumps(db)
        page_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Ticket Dashboard</title>
            <style>
                :root { color-scheme: dark; --bg: #07111f; --panel: #101826; --panel-2: #172233; --border: #243244; --text: #f8fafc; --muted: #94a3b8; --accent: #6366f1; --accent-2: #8b5cf6; --danger: #ef4444; --success: #22c55e; }
                * { box-sizing: border-box; }
                body { margin: 0; font-family: Inter, "Segoe UI", Roboto, Arial, sans-serif; background: linear-gradient(135deg, var(--bg), #111827); color: var(--text); }
                .shell { max-width: 1400px; margin: 0 auto; padding: 24px; }
                .topbar { display: flex; justify-content: space-between; align-items: center; padding: 18px 22px; border-radius: 16px; margin-bottom: 20px; background: rgba(16, 24, 38, 0.95); border: 1px solid var(--border); box-shadow: 0 10px 30px rgba(0,0,0,0.28); }
                .title-group h1 { margin: 0 0 4px; font-size: 24px; }
                .title-group p { margin: 0; color: var(--muted); }
                .badge { padding: 8px 12px; border-radius: 999px; font-size: 13px; background: rgba(99, 102, 241, 0.18); color: #c7d2fe; border: 1px solid rgba(99, 102, 241, 0.32); }
                .layout { display: grid; grid-template-columns: 1.05fr 0.95fr; gap: 20px; }
                .card { background: rgba(16, 24, 38, 0.95); border: 1px solid var(--border); border-radius: 16px; padding: 18px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
                .card h2 { margin-top: 0; font-size: 18px; }
                .card p { color: var(--muted); }
                .panel-list { display: grid; gap: 12px; }
                .panel-item { padding: 14px; border-radius: 12px; background: var(--panel-2); border: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; gap: 10px; }
                .panel-meta strong { display: block; margin-bottom: 4px; }
                .panel-meta span { color: var(--muted); font-size: 13px; }
                label { display: block; margin: 10px 0 6px; font-weight: 600; }
                input, textarea, select { width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border); background: #0b1220; color: var(--text); margin-bottom: 4px; }
                textarea { min-height: 94px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
                button { padding: 9px 12px; border: none; border-radius: 10px; margin: 6px 6px 0 0; cursor: pointer; color: white; font-weight: 600; }
                .btn-primary { background: linear-gradient(135deg, var(--accent), var(--accent-2)); }
                .btn-danger { background: linear-gradient(135deg, #f97316, var(--danger)); }
                .btn-success { background: linear-gradient(135deg, #10b981, #34d399); }
                .hint { background: rgba(99, 102, 241, 0.12); border: 1px solid rgba(99, 102, 241, 0.2); color: #dbeafe; padding: 12px; border-radius: 12px; margin-bottom: 14px; }
                .hint strong { color: #fff; }
                .preview-box { background: #0f172a; border: 1px solid var(--border); border-radius: 14px; padding: 14px; margin-top: 8px; }
                .embed-preview { background: #fff; color: #111827; border-radius: 12px; padding: 14px; box-shadow: 0 8px 20px rgba(0,0,0,0.2); }
                .embed-preview .title { font-size: 16px; font-weight: 700; margin-bottom: 6px; }
                .embed-preview .desc { white-space: pre-wrap; color: #374151; }
                .embed-preview .meta { margin-top: 8px; color: #6b7280; font-size: 12px; }
                .divider { height: 1px; background: var(--border); margin: 12px 0; }
                .checkbox-group { background: var(--panel-2); padding: 12px; border-radius: 10px; border: 1px solid var(--border); max-height: 200px; overflow-y: auto; }
                .checkbox-group label { display: flex; align-items: center; font-weight: normal; margin: 4px 0; cursor: pointer; }
                .checkbox-group input[type="checkbox"] { width: auto; margin: 0 10px 0 0; }
                @media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }
            </style>
        </head>
        <body>
            <div class="shell">
                <div class="topbar">
                    <div class="title-group">
                        <h1>Ticket Dashboard</h1>
                        <p>Advanced panel editor with JSON message payloads and live embed preview.</p>
                    </div>
                    <div class="badge">Tickety-style control panel</div>
                </div>
                <div class="layout">
                    <!-- LEFT COLUMN -->
                    <div>
                        <div class="card">
                            <h2>Existing panels</h2>
                            <p>Open, edit, deploy, or remove saved single ticket panels.</p>
                            <div id="panel-list" class="panel-list"></div>
                        </div>

                        <div class="card" style="margin-top: 20px;">
                            <h2>Existing Multi-Panels</h2>
                            <p>Dropdown menus containing multiple ticket options.</p>
                            <div id="mpanel-list" class="panel-list"></div>
                        </div>

                        <div class="card" style="margin-top: 20px;">
                            <h2>Create / edit multi-panel</h2>
                            <p>A single dropdown message that lets users pick from several panels.</p>
                            <label>Multi-Panel Name</label>
                            <input id="mp_name" />
                            <label>Default Deploy Channel ID (For 1-click deploy)</label>
                            <input id="mp_deploy_channel_id" placeholder="Channel ID for instant deployment" />
                            <label>Panels included in this dropdown</label>
                            <div id="mp_checkboxes" class="checkbox-group"></div>
                            <div class="divider"></div>
                            <label>Panel Message (Plain text or Discohook JSON)</label>
                            <textarea id="mp_message_json" rows="5" placeholder='{"content": "", "embeds": [{"title": "Select a ticket type", "description": "Choose an option from the dropdown below to open a ticket."}]}'></textarea>
                            <div class="divider"></div>
                            <button class="btn-success" onclick="saveMultiPanel()">Save Multi-Panel</button>
                            <button class="btn-danger" onclick="clearMultiForm()">Clear</button>
                        </div>
                    </div>

                    <!-- RIGHT COLUMN -->
                    <div>
                        <div class="card">
                            <h2>Create / edit panel</h2>
                            <div class="hint">
                                Login credentials: <strong>admin</strong> / <strong>__DASHBOARD_PASS__</strong>
                            </div>
                            <label>Panel Name</label>
                            <input id="name" />
                            <label>Channel Name Format</label>
                            <input id="channel_name_format" placeholder="e.g. ticket-{user}" />
                            <label>Category ID</label>
                            <input id="category_id" />
                            <label>Role ID</label>
                            <input id="role_id" />
                            <label>Default Deploy Channel ID (For 1-click deploy)</label>
                            <input id="deploy_channel_id" placeholder="Channel ID for instant deployment" />

                            <label>Welcome Message (Plain text or Discohook JSON)</label>
                            <textarea id="welcome_message" rows="5" placeholder='{"content": "Hey {user}!", "embeds": [{"title": "Welcome", "description": "Staff will be here soon."}]}'></textarea>

                            <label>Close Message</label>
                            <textarea id="close_message" rows="3"></textarea>

                            <label>Thread Message</label>
                            <textarea id="thread_message" rows="3"></textarea>

                            <label>Transcript Channel ID</label>
                            <input id="transcript_channel_id" />
                            <label>Panel Type</label>
                            <select id="type">
                                <option value="ticket">ticket</option>
                                <option value="application">application</option>
                            </select>
                            <div class="divider"></div>
                            <label>Message JSON</label>
                            <textarea id="message_json" placeholder='{ "content": "Welcome!", "embeds": [{ "title": "Support" }] }'></textarea>
                            <label>Embed JSON</label>
                            <textarea id="embed_json" placeholder='{ "title": "Support", "description": "Hello", "color": 16777215 }'></textarea>
                            <label>Questions JSON (For Forms)</label>
                            <textarea id="questions_json" placeholder='[{"type": "text", "label": "What do you need help with?", "required": true}]'></textarea>
                            <div class="divider"></div>
                            <h3>Embed Preview Editor</h3>
                            <label>Title</label>
                            <input id="embed_title" />
                            <label>Description</label>
                            <textarea id="embed_description"></textarea>
                            <label>Color</label>
                            <input id="embed_color" placeholder="e.g. 0x5865f2 or 5865f2" />
                            <label>Footer Text</label>
                            <input id="embed_footer" />
                            <label>Author Name</label>
                            <input id="embed_author" />
                            <label>Image URL</label>
                            <input id="embed_image" />
                            <label>Thumbnail URL</label>
                            <input id="embed_thumbnail" value="https://i.imgur.com/9KCE36G.png" />
                            <button class="btn-primary" onclick="syncEmbedFromFields()">Sync preview</button>
                            <button class="btn-success" onclick="applyEmbedJson()">Load JSON</button>
                            <div class="preview-box">
                                <div id="embed-preview" class="embed-preview"></div>
                            </div>
                            <div class="divider"></div>
                            <button class="btn-success" onclick="savePanel()">Save</button>
                            <button class="btn-danger" onclick="clearForm()">Clear</button>
                        </div>
                    </div>
                </div>
            </div>
            <script>
                const state = __DB_JSON__;
                let editingId = null;
                let editingMId = null;

                function renderPanels() {
                    const list = document.getElementById('panel-list');
                    const panels = state.panels || {};
                    if (!Object.keys(panels).length) {
                        list.innerHTML = '<div class="panel-item"><div class="panel-meta"><strong>No panels saved yet</strong><span>Create one from the form to begin.</span></div></div>';
                        return;
                    }
                    list.innerHTML = Object.entries(panels).map(([id, data]) => `
                        <div class="panel-item">
                            <div class="panel-meta">
                                <strong>${data.name || id}</strong>
                                <span>${data.type || 'ticket'} • Deploy Ch: ${data.deploy_channel_id || 'None'}</span>
                            </div>
                            <div>
                                <button class="btn-primary" onclick="editPanel('${id}')">Edit</button>
                                <button class="btn-danger" onclick="deletePanel('${id}')">Delete</button>
                                <button class="btn-success" onclick="deployPanel('${id}', '${data.deploy_channel_id || ''}')">Deploy</button>
                            </div>
                        </div>
                    `).join('');
                }

                function renderMultiPanelCheckboxes() {
                    const cbList = document.getElementById('mp_checkboxes');
                    const panels = state.panels || {};
                    const entries = Object.entries(panels);
                    if (!entries.length) {
                        cbList.innerHTML = '<label style="color:#94a3b8;">No panels created yet — create a panel first.</label>';
                        return;
                    }
                    cbList.innerHTML = entries.map(([id, data]) => `
                        <label>
                            <input type="checkbox" class="mp-panel-checkbox" value="${id}">
                            ${data.name || id}
                        </label>
                    `).join('');
                }

                function renderMultiPanels() {
                    const mList = document.getElementById('mpanel-list');
                    const mpanels = state.multi_panels || {};

                    if (!Object.keys(mpanels).length) {
                        mList.innerHTML = '<div class="panel-item"><div class="panel-meta"><strong>No multi-panels saved yet</strong></div></div>';
                    } else {
                        mList.innerHTML = Object.entries(mpanels).map(([id, data]) => `
                            <div class="panel-item">
                                <div class="panel-meta">
                                    <strong>${data.name || id}</strong>
                                    <span>${Object.keys(data.panels || {}).length} options linked • Deploy Ch: ${data.deploy_channel_id || 'None'}</span>
                                </div>
                                <div>
                                    <button class="btn-primary" onclick="editMultiPanel('${id}')">Edit</button>
                                    <button class="btn-danger" onclick="deleteMultiPanel('${id}')">Delete</button>
                                    <button class="btn-success" onclick="deployMultiPanel('${id}', '${data.deploy_channel_id || ''}')">Deploy</button>
                                </div>
                            </div>
                        `).join('');
                    }
                    renderMultiPanelCheckboxes();
                }

                function parseColor(value) {
                    if (!value) return 0x5865f2;
                    const cleaned = String(value).trim().replace(/^#/, '').replace(/^0x/, '');
                    if (/^[0-9a-fA-F]{6}$/.test(cleaned)) return parseInt(cleaned, 16);
                    const parsed = parseInt(cleaned, 10);
                    return Number.isNaN(parsed) ? 0x5865f2 : parsed;
                }

                function renderEmbedPreview() {
                    const title = document.getElementById('embed_title').value || 'Untitled embed';
                    const description = document.getElementById('embed_description').value || 'No description';
                    const footer = document.getElementById('embed_footer').value || '';
                    const author = document.getElementById('embed_author').value || '';
                    const image = document.getElementById('embed_image').value || '';
                    const thumbnail = document.getElementById('embed_thumbnail').value || '';
                    const color = parseColor(document.getElementById('embed_color').value);
                    const preview = document.getElementById('embed-preview');
                    preview.innerHTML = `
                        <div style="border-left: 6px solid #${color.toString(16).padStart(6, '0')}; padding-left: 10px;">
                            <div class="title">${title}</div>
                            <div class="desc">${description}</div>
                            ${author ? `<div class="meta">Author: ${author}</div>` : ''}
                            ${footer ? `<div class="meta">${footer}</div>` : ''}
                            ${image ? `<div style="margin-top: 10px;"><img src="${image}" style="max-width: 100%; border-radius: 8px;" /></div>` : ''}
                            ${thumbnail ? `<div style="margin-top: 10px;"><img src="${thumbnail}" style="max-width: 96px; border-radius: 8px;" /></div>` : ''}
                        </div>`;
                }

                function syncEmbedFromFields() {
                    const payload = {
                        title: document.getElementById('embed_title').value || '',
                        description: document.getElementById('embed_description').value || '',
                        color: parseColor(document.getElementById('embed_color').value),
                        footer: { text: document.getElementById('embed_footer').value || '' },
                        author: document.getElementById('embed_author').value ? { name: document.getElementById('embed_author').value } : null,
                        image: document.getElementById('embed_image').value ? { url: document.getElementById('embed_image').value } : null,
                        thumbnail: document.getElementById('embed_thumbnail').value ? { url: document.getElementById('embed_thumbnail').value } : null,
                    };
                    document.getElementById('embed_json').value = JSON.stringify(payload, null, 2);
                    renderEmbedPreview();
                }

                function applyEmbedJson() {
                    try {
                        const data = JSON.parse(document.getElementById('embed_json').value || '{}');
                        document.getElementById('embed_title').value = data.title || '';
                        document.getElementById('embed_description').value = data.description || '';
                        document.getElementById('embed_color').value = data.color || '';
                        document.getElementById('embed_footer').value = data.footer && data.footer.text ? data.footer.text : '';
                        document.getElementById('embed_author').value = data.author && data.author.name ? data.author.name : '';
                        document.getElementById('embed_image').value = data.image && data.image.url ? data.image.url : '';
                        document.getElementById('embed_thumbnail').value = data.thumbnail && data.thumbnail.url ? data.thumbnail.url : 'https://i.imgur.com/9KCE36G.png';
                        renderEmbedPreview();
                    } catch (err) {
                        alert('Invalid embed JSON.');
                    }
                }

                async function savePanel() {
                    let parsedQuestions = [];
                    try {
                        const qVal = document.getElementById('questions_json').value.trim();
                        if (qVal) parsedQuestions = JSON.parse(qVal);
                    } catch (e) {
                        alert("Invalid Questions JSON!");
                        return;
                    }

                    const payload = {
                        id: editingId,
                        data: {
                            name: document.getElementById('name').value,
                            channel_name_format: document.getElementById('channel_name_format').value,
                            type: document.getElementById('type').value,
                            category_id: document.getElementById('category_id').value,
                            role_id: document.getElementById('role_id').value,
                            deploy_channel_id: document.getElementById('deploy_channel_id').value,
                            welcome_message: document.getElementById('welcome_message').value,
                            close_message: document.getElementById('close_message').value,
                            thread_message: document.getElementById('thread_message').value,
                            transcript_channel_id: document.getElementById('transcript_channel_id').value,
                            pings: [],
                            questions: parsedQuestions,
                            message_json: document.getElementById('message_json').value,
                            embed_json: document.getElementById('embed_json').value,
                        }
                    };
                    const res = await fetch('/api/save_panel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
                    const json = await res.json();
                    if (json.success) {
                        window.location.reload();
                    }
                }

                async function deletePanel(id) {
                    await fetch('/api/delete_panel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
                    window.location.reload();
                }

                async function deployPanel(id, defaultChannelId) {
                    let channelId = defaultChannelId;
                    if (!channelId) {
                        channelId = prompt('Enter Channel ID to deploy to:');
                    }
                    if (!channelId) return;

                    const res = await fetch('/api/deploy', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: 'single', id, channel_id: channelId})});
                    const json = await res.json();
                    alert(json.message || 'Deployment requested');
                }

                function editPanel(id) {
                    editingId = id;
                    const data = state.panels[id] || {};
                    document.getElementById('name').value = data.name || '';
                    document.getElementById('channel_name_format').value = data.channel_name_format || '';
                    document.getElementById('category_id').value = data.category_id || '';
                    document.getElementById('role_id').value = data.role_id || '';
                    document.getElementById('deploy_channel_id').value = data.deploy_channel_id || '';
                    document.getElementById('welcome_message').value = data.welcome_message || '';
                    document.getElementById('close_message').value = data.close_message || '';
                    document.getElementById('thread_message').value = data.thread_message || '';
                    document.getElementById('transcript_channel_id').value = data.transcript_channel_id || '';
                    document.getElementById('type').value = data.type || 'ticket';
                    document.getElementById('message_json').value = data.message_json || '{"content":"", "embeds": []}';
                    document.getElementById('embed_json').value = data.embed_json || '{"title":"","description":"","color": 0x5865f2}';
                    document.getElementById('questions_json').value = data.questions && data.questions.length ? JSON.stringify(data.questions, null, 2) : '';
                    applyEmbedJson();
                }

                function clearForm() {
                    editingId = null;
                    document.getElementById('name').value = '';
                    document.getElementById('channel_name_format').value = '';
                    document.getElementById('category_id').value = '';
                    document.getElementById('role_id').value = '';
                    document.getElementById('deploy_channel_id').value = '';
                    document.getElementById('welcome_message').value = '';
                    document.getElementById('close_message').value = '';
                    document.getElementById('thread_message').value = '';
                    document.getElementById('transcript_channel_id').value = '';
                    document.getElementById('type').value = 'ticket';
                    document.getElementById('message_json').value = '{"content":"", "embeds": []}';
                    document.getElementById('embed_json').value = '{"title":"","description":"","color": 0x5865f2}';
                    document.getElementById('questions_json').value = '';
                    renderEmbedPreview();
                }

                async function saveMultiPanel() {
                    const selected = Array.from(document.querySelectorAll('.mp-panel-checkbox:checked')).map(cb => cb.value);
                    if (!selected.length) {
                        alert('Select at least one panel to include.');
                        return;
                    }
                    const panelsObj = {};
                    selected.forEach(id => { panelsObj[id] = true; });

                    const payload = {
                        id: editingMId,
                        data: {
                            name: document.getElementById('mp_name').value,
                            deploy_channel_id: document.getElementById('mp_deploy_channel_id').value,
                            panels: panelsObj,
                            message_json: document.getElementById('mp_message_json').value,
                        }
                    };
                    const res = await fetch('/api/save_multi_panel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
                    const json = await res.json();
                    if (json.success) {
                        window.location.reload();
                    }
                }

                async function deleteMultiPanel(id) {
                    await fetch('/api/delete_multi_panel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
                    window.location.reload();
                }

                async function deployMultiPanel(id, defaultChannelId) {
                    let channelId = defaultChannelId;
                    if (!channelId) {
                        channelId = prompt('Enter Channel ID to deploy to:');
                    }
                    if (!channelId) return;

                    const res = await fetch('/api/deploy', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: 'multi', id, channel_id: channelId})});
                    const json = await res.json();
                    alert(json.message || 'Deployment requested');
                }

                function editMultiPanel(id) {
                    editingMId = id;
                    const data = (state.multi_panels || {})[id] || {};
                    document.getElementById('mp_name').value = data.name || '';
                    document.getElementById('mp_deploy_channel_id').value = data.deploy_channel_id || '';
                    document.getElementById('mp_message_json').value = data.message_json || '';
                    renderMultiPanelCheckboxes();
                    const included = Object.keys(data.panels || {});
                    document.querySelectorAll('.mp-panel-checkbox').forEach(cb => {
                        cb.checked = included.includes(cb.value);
                    });
                }

                function clearMultiForm() {
                    editingMId = null;
                    document.getElementById('mp_name').value = '';
                    document.getElementById('mp_deploy_channel_id').value = '';
                    document.getElementById('mp_message_json').value = '';
                    renderMultiPanelCheckboxes();
                }

                renderPanels();
                renderMultiPanels();
                clearForm();
                clearMultiForm();
            </script>
        </body>
        </html>
        """
        page_html = page_html.replace("__DB_JSON__", db_json).replace("__DASHBOARD_PASS__", WEB_SECRET)
        return web.Response(text=page_html, content_type="text/html")

    async def api_save_panel(self, request):
        data = await request.json()
        pid = data.get("id") or str(uuid.uuid4())[:8]
        self.panels[pid] = data.get("data", {})
        db["panels"][pid] = self.panels[pid]
        save_db(db)
        return web.json_response({"success": True, "id": pid})

    async def api_delete_panel(self, request):
        data = await request.json()
        pid = data.get("id")
        if pid in self.panels:
            del self.panels[pid]
            db["panels"].pop(pid, None)
            save_db(db)
        return web.json_response({"success": True})

    async def api_save_multi_panel(self, request):
        data = await request.json()
        mpid = data.get("id") or str(uuid.uuid4())[:8]
        self.multi_panels[mpid] = data.get("data", {})
        db["multi_panels"][mpid] = self.multi_panels[mpid]
        save_db(db)
        return web.json_response({"success": True, "id": mpid})

    async def api_delete_multi_panel(self, request):
        data = await request.json()
        mpid = data.get("id")
        if mpid in self.multi_panels:
            del self.multi_panels[mpid]
            db["multi_panels"].pop(mpid, None)
            save_db(db)
        return web.json_response({"success": True})

    async def api_deploy(self, request):
        data = await request.json()
        mode = data.get("mode")
        item_id = data.get("id")
        channel_id = data.get("channel_id")
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                return web.json_response({"message": "Error: Invalid channel ID."})

            if mode == "single":
                pdata = self.panels.get(item_id)
                if not pdata:
                    return web.json_response({"message": "Panel not found."})

                payload = parse_json_field(pdata.get("message_json")) or {}
                content = payload.get("content") or None

                embeds = []
                for e_data in payload.get("embeds", []):
                    try:
                        embeds.append(discord.Embed.from_dict(e_data))
                    except Exception:
                        pass

                if not embeds and not content:
                    embed = discord.Embed(color=COLOUR_BRAND)
                    embed.title = pdata.get("name", "Ticket Panel")
                    embed.description = pdata.get("welcome_message", "Click below to open.")
                    embeds.append(embed)

                view = DynamicPanelView(self.bot, item_id, pdata)
                await channel.send(content=content, embeds=embeds[:10], view=view)

            elif mode == "multi":
                mdata = self.multi_panels.get(item_id)
                if not mdata:
                    return web.json_response({"message": "Multi-panel not found."})

                linked_panels = {pid: self.panels[pid] for pid in mdata.get("panels", {}) if pid in self.panels}
                if not linked_panels:
                    return web.json_response({"message": "This multi-panel has no valid panels linked to it."})

                payload = parse_json_field(mdata.get("message_json")) or {}
                content = payload.get("content") or None

                embeds = []
                for e_data in payload.get("embeds", []):
                    try:
                        embeds.append(discord.Embed.from_dict(e_data))
                    except Exception:
                        pass

                if not embeds and not content:
                    embed = discord.Embed(color=COLOUR_BRAND)
                    embed.title = mdata.get("name", "Select a ticket type")
                    embed.description = "Choose an option from the dropdown below to open a ticket."
                    embeds.append(embed)

                view = DynamicMultiPanelView(self.bot, linked_panels)
                await channel.send(content=content, embeds=embeds[:10], view=view)
            else:
                return web.json_response({"message": f"Unknown deploy mode: {mode}"})

            return web.json_response({"message": "Successfully deployed."})
        except Exception as exc:
            return web.json_response({"message": f"Deployment failed: {exc}"})


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))