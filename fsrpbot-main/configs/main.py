# ── Main server config ────────────────────────────────────────
BOT_TOKEN = "[Put bot token here]"
BOT_NAME  = "FSRP Main"
GUILD_ID  = 1234567890
IS_DEPT   = False

OWNERSHIP_ROLE_ID        = 1234567890
DIRECTIVE_TEAM_ROLE_ID   = 1234567890
MANAGEMENT_TEAM_ROLE_ID  = 1234567890
INTERNAL_AFFAIRS_ROLE_ID = 1234567890
ADMINISTRATION_ROLE_ID   = 1234567890
MODERATION_ROLE_ID       = 1234567890

STAFF_HIERARCHY = [
    OWNERSHIP_ROLE_ID,
    DIRECTIVE_TEAM_ROLE_ID,
    MANAGEMENT_TEAM_ROLE_ID,
    INTERNAL_AFFAIRS_ROLE_ID,
    ADMINISTRATION_ROLE_ID,
    MODERATION_ROLE_ID,
]
ALL_STAFF_ROLE_IDS = STAFF_HIERARCHY.copy()
FORMER_STAFF_ROLE_ID = 1234567890

# ── Warning / Strike roles ────────────────────────────────────
WARNING_1_ROLE_ID = 1234567890
WARNING_2_ROLE_ID = 1234567890
STRIKE_1_ROLE_ID  = 1234567890
STRIKE_2_ROLE_ID  = 1234567890

# ── Channels ──────────────────────────────────────────────────
INFRACTION_LOG_CHANNEL_ID  = 1234567890
SESSION_CHANNEL_ID         = 1234567890
PROMOTION_LOG_CHANNEL_ID   = 1234567890
RESIGNATION_LOG_CHANNEL_ID = 1234567890
FEEDBACK_LOG_CHANNEL_ID    = 1234567890
BOT_LOG_CHANNEL_ID         = 1234567890
SESSION_PING_ROLE_ID       = 1234567890

# ── APIs ──────────────────────────────────────────────────────
ERLC_GLOBAL_API_KEY  = "[api key]"
ERLC_SERVER_KEY = "[server key]"
ERLC_API_BASE    = "https://api.erlc.gg"
MELONLY_API_KEY  = "[melonly api]"

# ── Session thresholds ────────────────────────────────────────
SESSION_FULL_THRESHOLD  = 49
SESSION_BOOST_THRESHOLD = 25
SESSION_CHECK_INTERVAL  = 15

# ── Infraction types ─────────────────────────────────────────
INFRACTION_TYPES = [
    "Warning", "Strike", "Under Investigation",
    "Suspension", "Demotion", "Termination", "Blacklist",
]

# ── Infraction issuer roles (all roles that can run infraction commands) ──
INFRACTION_ISSUER_ROLE_IDS = [
    OWNERSHIP_ROLE_ID,
    DIRECTIVE_TEAM_ROLE_ID,
    MANAGEMENT_TEAM_ROLE_ID,
    INTERNAL_AFFAIRS_ROLE_ID,
]

DEPARTMENT_SERVERS = {
    "mpd": {
        "guild_id": 1234567890
    },
    "mdfr": {
        "guild_id": 1234567890
    },
    "fhp": {
        "guild_id": 1234567890
    },
    "mdso": {
        "guild_id": 1234567890
    },
    "fdot": {
        "guild_id": v
    },
}

# ── Colours / images ──────────────────────────────────────────
COLOUR_BRAND   = 0xF59E2B
COLOUR_OK      = 0x10b981
COLOUR_WARN    = 0xF59E2B
COLOUR_ERROR   = 0xef4444
COLOUR_INFO    = 0x3b82f6
COLOUR_NEUTRAL = 0x6b7280
EMBED_IMAGE_PROMOTION   = "https://i.imgur.com/2L9lu34.png"
EMBED_IMAGE_INFRACTION  = "https://i.imgur.com/SFX5VSt.png"
EMBED_IMAGE_RESIGNATION = "https://i.imgur.com/rBiTOpd.png"
EMBED_IMAGE_FEEDBACK    = "https://i.imgur.com/1qEUMNE.png"

# ── OpenRouter (semantic auto-reply embeddings) ───────────────
# Get a key at https://openrouter.ai/keys — put the real value here.
OPENROUTER_API_KEY = "[AI api key]"
# nvidia/llama-nemotron-embed-vl-1b-v2:free is a genuinely $0/M-token
# embedding model on OpenRouter (see https://openrouter.ai/collections/embedding-models).
# Free-tier models are rate limited (roughly 20 req/min, ~1000 req/day at
# time of writing — check OpenRouter's own docs for current numbers), and
# autoreply.py calls this on every non-command message, so on an active
# server you may hit that ceiling. Swap this for a paid embedding model
# (e.g. "openai/text-embedding-3-small") if that becomes a problem.
OPENROUTER_EMBEDDING_MODEL = "[AI model path]"
