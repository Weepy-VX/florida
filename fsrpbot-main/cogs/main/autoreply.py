import os
import json
import re
from pathlib import Path
from typing import Optional, Tuple, Dict, List
import aiohttp
import discord
from discord.ext import commands

from configs.main import OPENROUTER_API_KEY, OPENROUTER_EMBEDDING_MODEL
from utils.helpers import is_senior_hr, err_embed

# ==========================================
# 1. CLOUD-BASED EMEDDING MANAGER
# ==========================================
_DEFAULT_REPLIES_PATH = Path(__file__).resolve().parent.parent / "data" / "autoreplies.json"


class OpenRouterEmbeddingManager:
    def __init__(self, filepath: str | Path = _DEFAULT_REPLIES_PATH, strict_threshold: float = 0.90, review_threshold: float = 0.80):
        self.filepath = Path(filepath)
        self.strict_threshold = strict_threshold
        self.review_threshold = review_threshold
        self.replies: Dict[str, str] = {}

        # OpenRouter API configuration — both the key and which (free)
        # model to use live in configs/main.py, same as every other API
        # key in this bot (MELONLY_API_KEY, ERLC_SERVER_KEY, etc.).
        self.api_key = OPENROUTER_API_KEY
        # The real embeddings endpoint (OpenAI-compatible schema) — see
        # https://openrouter.ai/docs/api_reference/embeddings. The old
        # code posted straight to "https://openrouter.ai", which isn't a
        # valid API route at all.
        self.api_url = "https://openrouter.ai/api/v1/embeddings"
        self.model_name = OPENROUTER_EMBEDDING_MODEL

        self.load_from_json()

    def load_from_json(self) -> None:
        """Loads stored triggers safely from a local JSON file."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.replies = json.load(f)
            except Exception as e:
                print(f"Error loading JSON file: {e}")
                self.replies = {}
        else:
            self.replies = {}

    def save_to_json(self) -> None:
        """Saves current state to the configured JSON path."""
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.replies, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving to JSON file: {e}")

    def add_reply(self, trigger: str, response: str) -> None:
        """Stores a new trigger and saves state to disk."""
        self.replies[trigger] = response
        self.save_to_json()

    async def _get_embeddings(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Fetches vector embeddings from OpenRouter via async requests."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model_name,
            "input": texts
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [item["embedding"] for item in data["data"]]
                    else:
                        print(f"OpenRouter API Error: {resp.status} - {await resp.text()}")
                        return None
        except Exception as e:
            print(f"Failed to fetch embeddings: {e}")
            return None

    def _cosine_similarity(self, vec_a: List[float], vec_b: List[float]) -> float:
        """Calculates similarity math between two matching vector strings."""
        dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = sum(a * a for a in vec_a) ** 0.5
        norm_b = sum(b * b for b in vec_b) ** 0.5
        if not norm_a or not norm_b:
            return 0.0
        return dot_product / (norm_a * norm_b)

    async def find_matches(self, user_message: str) -> List[Tuple[str, str, float]]:
        """Evaluates message similarities using OpenRouter cloud endpoints."""
        if not self.replies:
            return []

        triggers = list(self.replies.keys())
        all_texts = [user_message] + triggers
        
        # Pull vector metrics from cloud
        embeddings = await self._get_embeddings(all_texts)
        if not embeddings:
            return []

        message_vector = embeddings[0]
        trigger_vectors = embeddings[1:]

        candidates = []
        for idx, t_vec in enumerate(trigger_vectors):
            score = self._cosine_similarity(message_vector, t_vec)
            candidates.append((triggers[idx], self.replies[triggers[idx]], score))

        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates

    def delete_reply(self, trigger: str) -> bool:
        """Removes an auto-reply and updates persistent storage."""
        if trigger in self.replies:
            del self.replies[trigger]
            self.save_to_json()
            return True
        return False

    def delete_all(self) -> int:
        """Clears all stored configuration pairs from disk."""
        count = len(self.replies)
        self.replies.clear()
        self.save_to_json()
        return count

    def grouped_replies(self) -> List[Tuple[str, List[str]]]:
        """Groups triggers that share an identical response together, so a
        listing shows one entry per unique reply instead of one per
        trigger. This matters a lot in practice because the review-queue
        approval flow (ReviewApprovalView) keeps adding new trigger
        variants that all point at the same existing response — without
        grouping, -listreplies burns a field (and a page) per variant for
        what is really one configured reply. Sorted by trigger count
        (most-combined first) so the biggest space savings show up first.
        """
        grouped: Dict[str, List[str]] = {}
        for trigger, response in self.replies.items():
            grouped.setdefault(response, []).append(trigger)
        return sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0]))


# ==========================================
# 2. DISCORD INTERACTION VIEWS
# ==========================================
class ConfirmationView(discord.ui.View):
    def __init__(self, user: discord.User, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.user = user
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message("❌ This confirmation dialog belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()


class ReviewApprovalView(discord.ui.View):
    def __init__(self, manager: OpenRouterEmbeddingManager, trigger: str, response: str, ping_id: int):
        super().__init__(timeout=86400.0)
        self.manager = manager
        self.trigger = trigger
        self.response = response
        self.ping_id = ping_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not is_senior_hr(interaction.user):
            await interaction.response.send_message(
                "❌ You need the **Directive Team** role or higher to configure auto-replies.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Approve & Save", style=discord.ButtonStyle.success, custom_id="approve_reply")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.manager.add_reply(self.trigger, self.response)
        
        embed = discord.Embed(
            title="✅ Auto-Reply Review Approved",
            description=f"Approved by {interaction.user.mention}.",
            color=discord.Color.green()
        )
        embed.add_field(name="Trigger Context", value=f"\"{self.trigger}\"", inline=False)
        embed.add_field(name="Assigned Response", value=f"\"{self.response}\"", inline=False)
        
        for child in self.children:
            child.disabled = True
        
        await interaction.response.edit_message(content=None, embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="reject_reply")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="❌ Auto-Reply Review Rejected",
            description=f"Rejected by {interaction.user.mention}.",
            color=discord.Color.red()
        )
        for child in self.children:
            child.disabled = True
            
        await interaction.response.edit_message(content=None, embed=embed, view=self)
        self.stop()


class ReplyPaginationView(discord.ui.View):
    def __init__(self, items: List[Tuple[str, List[str]]], per_page: int = 25):
        """items: [(response, [triggers...]), ...] — see
        OpenRouterEmbeddingManager.grouped_replies()."""
        super().__init__(timeout=60.0)
        self.items = items
        self.per_page = per_page
        self.current_page = 0
        self.max_page = max(0, (len(items) - 1) // per_page)
        self.update_buttons()

    @staticmethod
    def _format_triggers(triggers: List[str], limit: int = 220) -> str:
        """Joins trigger variants into one compact, comma-separated line,
        truncating with a "+N more" marker rather than letting a reply
        with many trigger variants blow past Discord's 1024-char field
        value limit."""
        shown: List[str] = []
        length = 0
        for t in triggers:
            piece = f"`{t}`"
            added = len(piece) + (2 if shown else 0)  # ", " separator
            if length + added > limit and shown:
                break
            shown.append(piece)
            length += added
        text = ", ".join(shown)
        remaining = len(triggers) - len(shown)
        if remaining > 0:
            text += f" *(+{remaining} more)*"
        return text

    def get_page_embed(self) -> discord.Embed:
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_items = self.items[start:end]

        total_triggers = sum(len(triggers) for _, triggers in self.items)
        embed = discord.Embed(
            title="Configured Auto Replies",
            description=(
                f"Showing page {self.current_page + 1} of {self.max_page + 1} — "
                f"{len(self.items)} unique replies, {total_triggers} trigger variants total"
            ),
            color=discord.Color.blue()
        )

        for idx, (response, triggers) in enumerate(page_items, start=start + 1):
            label = "trigger" if len(triggers) == 1 else "triggers"
            embed.add_field(
                name=f"{idx}. Reply ({len(triggers)} {label})",
                value=f"**Triggers:** {self._format_triggers(triggers)}\n**Reply:** {response}",
                inline=False
            )
        return embed

    def update_buttons(self):
        self.prev_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page == self.max_page

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_page_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_page_embed(), view=self)


# ==========================================
# 3. DISCORD COG MODULE
# ==========================================

# Categories the autoreply should never fire in at all (support/staff
# channels etc. where it'd just get in the way).
EXCLUDED_CATEGORY_IDS = {
    1531465336178741340,
    1532141840382300212,
    1533608689901109299,
    1531417119575773435,
}

# Cheap, dependency-free "is this a question" heuristic — deliberately not
# another embeddings call, to avoid doubling the already-tight free-tier
# rate limit. Not perfect, but it's enough to stop the bot from firing on
# a plain statement (e.g. staff already answering someone) just because
# the wording happens to be semantically close to a stored trigger.
_QUESTION_STARTERS = {
    "who", "what", "when", "where", "why", "how", "which", "whose", "whom",
    "is", "are", "am", "was", "were", "do", "does", "did",
    "can", "could", "would", "should", "will", "shall", "may", "might",
    "has", "have", "had",
    "isn't", "aren't", "wasn't", "weren't", "don't", "doesn't", "didn't",
    "can't", "cannot", "couldn't", "wouldn't", "shouldn't", "won't",
    "haven't", "hasn't", "hadn't",
}
_QUESTION_ADJACENT_PHRASES = (
    "anyone know", "does anyone", "can someone", "could someone", "does someone",
    "need help", "needs help", "need some help", "help with", "help me",
    "having trouble", "having troubles", "having issues", "having an issue", "having a problem",
    "not sure how", "not sure if", "not sure what", "no idea how", "no clue how",
    "how do i", "how to", "i'm stuck", "im stuck", "i am stuck", "i'm confused", "im confused",
    "getting an error", "keeps erroring", "keep getting", "any idea",
)
_LEADING_JUNK_RE = re.compile(r"^(?:<@!?\d+>|<a?:\w+:\d+>|\s|[\"'*_~`])+")
_TRAILING_JUNK = " \t\n\"'*_~`)]}"


def _looks_like_question(content: str) -> bool:
    """True if the message reads as a question or a request for help,
    False for plain statements — e.g. someone typing out an answer/
    explanation to another user, which shouldn't trigger an autoreply
    just because the wording is semantically close to a stored trigger."""
    text = content.strip()
    if not text:
        return False

    if text.rstrip(_TRAILING_JUNK).endswith("?"):
        return True

    stripped = _LEADING_JUNK_RE.sub("", text).lower()
    first_word = re.split(r"[\s,]", stripped, maxsplit=1)[0].strip("¿!.,;:") if stripped else ""
    if first_word in _QUESTION_STARTERS or first_word.split("'")[0] in _QUESTION_STARTERS:
        return True

    return any(phrase in stripped for phrase in _QUESTION_ADJACENT_PHRASES)


def _message_category_id(channel) -> int | None:
    """Resolves a channel's parent category ID, working for both regular
    channels and threads (whose category lives on their parent channel)."""
    cat_id = getattr(channel, "category_id", None)
    if cat_id is not None:
        return cat_id
    parent = getattr(channel, "parent", None)
    return getattr(parent, "category_id", None) if parent is not None else None


class SemanticAutoReplyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.manager = OpenRouterEmbeddingManager(
            strict_threshold=0.90,
            review_threshold=0.80,
        )
        self.review_channel_id = 1531606177979564133
        self.ping_role_or_user_id = 637836453983813653

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if _message_category_id(message.channel) in EXCLUDED_CATEGORY_IDS:
            return

        if not _looks_like_question(message.content):
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        candidates = await self.manager.find_matches(message.content)
        if not candidates:
            return

        best_trigger, best_response, best_score = candidates[0]

        if best_score >= self.manager.strict_threshold:
            await message.reply(best_response, mention_author=True)
            return

        if self.manager.review_threshold <= best_score < self.manager.strict_threshold:
            review_channel = self.bot.get_channel(self.review_channel_id)
            if review_channel:
                guild = message.guild
                ping_mention = f"<@&{self.ping_role_or_user_id}>" if guild and guild.get_role(self.ping_role_or_user_id) else f"<@{self.ping_role_or_user_id}>"

                embed = discord.Embed(title="⚠️ Auto-Reply Review Request", description=f"A message triggered a {best_score:.1%} semantic match.", color=discord.Color.orange())
                embed.add_field(name="User's Message Input", value=f"\"{message.content}\"", inline=False)
                embed.add_field(name="Closest Existing Trigger Match", value=f"\"{best_trigger}\"", inline=False)
                embed.add_field(name="Configured Target Reply", value=f"\"{best_response}\"", inline=False)
                embed.set_footer(text="If approved, this new trigger variant will be saved to storage and automatically replied to next time.")

                view = ReviewApprovalView(self.manager, message.content, best_response, self.ping_role_or_user_id)
                await review_channel.send(content=f"{ping_mention} Review Required!", embed=embed, view=view)

    @commands.command(name="addreply")
    async def add_reply_cmd(self, ctx: commands.Context, trigger: str, response: str):
        """Configures an auto-reply. Usage: -addreply "Trigger text" "Target reply" """
        if not ctx.guild or not is_senior_hr(ctx.author):
            await ctx.reply(embed=err_embed("You need the **Directive Team** role or higher to configure auto-replies."), mention_author=True)
            return
        self.manager.add_reply(trigger, response)
        await ctx.reply(f"✅ Saved configurations directly to storage!\nTrigger: {trigger}\nResponse: {response}", mention_author=True)

    @commands.command(name="delreply")
    async def del_reply_cmd(self, ctx: commands.Context, trigger_input: str):
        """Removes matching auto-reply configurations semantically."""
        if not ctx.guild or not is_senior_hr(ctx.author):
            await ctx.reply(embed=err_embed("You need the **Directive Team** role or higher to configure auto-replies."), mention_author=True)
            return
        candidates = await self.manager.find_matches(trigger_input)
        if not candidates:
            await ctx.reply("❌ No matching configuration was located within storage parameters.", mention_author=True)
            return

        best_trigger, response, score = candidates[0]

        if score < self.manager.review_threshold:
            await ctx.reply("❌ Closest match is below acceptable confidence levels. Nothing deleted.", mention_author=True)
            return

        view = ConfirmationView(ctx.author)
        confirm_text = f"⚠️ Structural match located with confidence level {score:.1%}. Confirm delete?\nTrigger: {best_trigger}\nReply: {response}"
        msg = await ctx.reply(confirm_text, view=view, mention_author=True)
        await view.wait()

        if view.value is True:
            self.manager.delete_reply(best_trigger)
            await msg.edit(content="✅ Entry removed from persistent JSON records.", view=None)
        else:
            await msg.edit(content="❌ Deletion window aborted safely.", view=None)

    @commands.command(name="delallreplies")
    async def del_all_replies_cmd(self, ctx: commands.Context):
        """Clears all configured entries entirely from disk."""
        if not ctx.guild or not is_senior_hr(ctx.author):
            await ctx.reply(embed=err_embed("You need the **Directive Team** role or higher to configure auto-replies."), mention_author=True)
            return
        total_count = len(self.manager.replies)
        if total_count == 0:
            await ctx.reply("❌ There are zero configuration metrics currently initialized on disk.", mention_author=True)
            return

        view = ConfirmationView(ctx.author)
        msg = await ctx.reply(f"⚠️ Are you certain you wish to wipe all {total_count} configurations from memory and files?", view=view, mention_author=True)
        await view.wait()

        if view.value is True:
            self.manager.delete_all()
            await msg.edit(content="✅ JSON configuration successfully initialized to a blank database state.", view=None)
        else:
            await msg.edit(content="❌ Bulk removal profile stopped.", view=None)

    @commands.command(name="listreplies")
    async def list_replies_cmd(self, ctx: commands.Context):
        """Renders paginated visual layout matrices of active setups."""
        items = self.manager.grouped_replies()
        if not items:
            await ctx.reply("ℹ️ No configuration models exist inside the local database storage module.", mention_author=True)
            return

        view = ReplyPaginationView(items, per_page=25)
        await ctx.reply(embed=view.get_page_embed(), view=view, mention_author=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(SemanticAutoReplyCog(bot))