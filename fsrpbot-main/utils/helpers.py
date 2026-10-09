"""
utils/helpers.py — permission checks, embed builders, API calls.
"""
import discord
import aiohttp
import time
import asyncio
import json
from pathlib import Path
from configs.main import *


def _count_staff_members(staff_payload: object) -> int:
    if isinstance(staff_payload, dict):
        for group in ("Admins", "Mods", "Helpers"):
            bucket = staff_payload.get(group)
            if isinstance(bucket, dict):
                return len(bucket)
            if isinstance(bucket, list):
                return len(bucket)

        if not staff_payload:
            return 0

        total = 0
        for value in staff_payload.values():
            if isinstance(value, dict):
                total += len(value)
            elif isinstance(value, list):
                total += len(value)
        return total

    if isinstance(staff_payload, list):
        return len(staff_payload)

    return 0


def _count_staff_in_game(players_payload: object) -> int:
    """Staff currently ONLINE, derived from each entry's `Permission`
    field in the Players list.

    This is intentionally NOT based on the `Staff` field from the API —
    `Staff` (Admins/Mods/Helpers) is the full roster of everyone who HAS a
    staff rank on the server, regardless of whether they're in-game right
    now. Using that for a "staff in-game" count is why it used to show a
    number like 68 on an empty server. A player is only counted here if
    they're actually present in `Players` with a non-"Normal" permission.
    """
    if not isinstance(players_payload, list):
        return 0
    count = 0
    for player in players_payload:
        if not isinstance(player, dict):
            continue
        permission = player.get("Permission") or player.get("permission")
        if permission and permission != "Normal":
            count += 1
    return count


def _extract_player_count(payload: object) -> int:
    if isinstance(payload, dict):
        for key in ("CurrentPlayers", "currentPlayers", "Players"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
            if isinstance(value, list):
                return len(value)
    return 0


def _extract_queue_count(payload: object) -> int:
    if isinstance(payload, dict):
        for key in ("Queue", "queue"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return 0

START_TIME = time.time()
_ROBLOX_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "roblox_cache.json"
_ROBLOX_CACHE: dict[str, dict] | None = None

# =============================================================
# PERMISSION HELPERS
# =============================================================

def _has_role(member: discord.Member, *role_ids: int) -> bool:
    ids = {r.id for r in member.roles}
    return bool(ids & set(role_ids))

def is_senior_hr(member: discord.Member) -> bool:
    return _has_role(member, OWNERSHIP_ROLE_ID, DIRECTIVE_TEAM_ROLE_ID)

def is_hr(member: discord.Member) -> bool:
    return _has_role(member, OWNERSHIP_ROLE_ID, DIRECTIVE_TEAM_ROLE_ID, MANAGEMENT_TEAM_ROLE_ID)

def is_admin(member: discord.Member) -> bool:
    return _has_role(member, OWNERSHIP_ROLE_ID, DIRECTIVE_TEAM_ROLE_ID, MANAGEMENT_TEAM_ROLE_ID,
                     INTERNAL_AFFAIRS_ROLE_ID, ADMINISTRATION_ROLE_ID)

def is_ia(member: discord.Member) -> bool:
    return _has_role(member, OWNERSHIP_ROLE_ID, DIRECTIVE_TEAM_ROLE_ID, MANAGEMENT_TEAM_ROLE_ID,
                     INTERNAL_AFFAIRS_ROLE_ID)

def is_staff(member: discord.Member) -> bool:
    return _has_role(member, OWNERSHIP_ROLE_ID, DIRECTIVE_TEAM_ROLE_ID, MANAGEMENT_TEAM_ROLE_ID,
                     INTERNAL_AFFAIRS_ROLE_ID, ADMINISTRATION_ROLE_ID, MODERATION_ROLE_ID)

def get_hierarchy_index(member: discord.Member) -> int:
    member_role_ids = {r.id for r in member.roles}
    for i, role_id in enumerate(STAFF_HIERARCHY):
        if role_id in member_role_ids:
            return i
    return len(STAFF_HIERARCHY)

def can_promote_to(promoter: discord.Member, target_role: discord.Role) -> bool:
    promoter_idx = get_hierarchy_index(promoter)
    try:
        target_idx = STAFF_HIERARCHY.index(target_role.id)
    except ValueError:
        promoter_top = max(
            (r.position for r in promoter.roles if r.id in STAFF_HIERARCHY),
            default=0
        )
        return promoter_top > target_role.position
    if promoter_idx < target_idx:
        return True
    if promoter_idx == target_idx:
        promoter_top = max(
            (r.position for r in promoter.roles if r.id in STAFF_HIERARCHY),
            default=0
        )
        return promoter_top > target_role.position
    return False

# =============================================================
# EMBED BUILDERS
# =============================================================

def ok_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOUR_BRAND)

def err_embed(description: str) -> discord.Embed:
    return discord.Embed(title="Error", description=description, color=COLOUR_BRAND)

def info_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOUR_BRAND)

def neutral_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOUR_BRAND)

# =============================================================
# ER:LC API
# =============================================================

ERLC_HEADERS = {"server-key": ERLC_SERVER_KEY}
ERLC_TIMEOUT = aiohttp.ClientTimeout(total=8)

# ── Rate-limit tracking ───────────────────────────────────────
# ER:LC returns X-RateLimit-Bucket / -Limit / -Remaining / -Reset on every
# response (see https://apidocs.erlc.gg/rate-limits). State is keyed by the
# real bucket name so the "global" bucket (status/players/etc.) and the
# separate "command-<server-key>" bucket are throttled independently. On a
# 429 we stop hitting that bucket entirely until Retry-After elapses —
# ER:LC explicitly requires this to avoid escalating to a temporary/
# permanent IP block.
_ERLC_RATE_LOCK = asyncio.Lock()
_ERLC_RATE_STATE: dict[str, dict] = {}


def _erlc_guess_bucket(endpoint: str) -> str:
    """Best-effort bucket name before we've seen a real response for this
    endpoint. Refined immediately after from the actual header."""
    return "command" if endpoint.endswith("/command") else "global"


async def _erlc_rate_wait(bucket: str) -> None:
    async with _ERLC_RATE_LOCK:
        state = _ERLC_RATE_STATE.get(bucket)
        wait_s = 0.0
        if state:
            now = time.time()
            if state.get("remaining", 1) <= 0 and state.get("reset_at", 0) > now:
                wait_s = state["reset_at"] - now
    if wait_s > 0:
        await asyncio.sleep(wait_s)


def _erlc_rate_update(headers) -> str:
    bucket = headers.get("X-RateLimit-Bucket") or "global"
    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if remaining is not None or reset is not None:
        state = _ERLC_RATE_STATE.setdefault(bucket, {})
        if remaining is not None:
            try:
                state["remaining"] = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                state["reset_at"] = float(reset)
            except ValueError:
                pass
    return bucket


def _erlc_rate_block(bucket: str, retry_after: float) -> None:
    state = _ERLC_RATE_STATE.setdefault(bucket, {})
    state["remaining"] = 0
    state["reset_at"] = time.time() + max(retry_after, 0.0)

async def _erlc_request(
    method: str, endpoint: str, *, params: dict | None = None, payload: dict | None = None
) -> tuple[int, dict | list | None, int | None]:
    """Low-level request. Returns (http_status, body, error_code).

    `error_code` is ER:LC's own numeric code from the JSON error body
    (https://apidocs.erlc.gg/error-codes) — NOT the HTTP status. It's the
    only reliable way to tell these apart, since several of them can share
    the same HTTP status:
      3002 = server is legitimately offline (has no players) — expected,
             not a failure.
      4001 = rate limited (also usually HTTP 429).
      2000-2004 = server-key / global API key problem.
      4002/4003 = the command or message itself was rejected.
      9999 = the in-game module is outdated; needs a player kick to clear.
    `body` is the parsed JSON on success (HTTP 200), otherwise None.
    """
    url = f"{ERLC_API_BASE}{endpoint}"
    await _erlc_rate_wait(_erlc_guess_bucket(endpoint))
    try:
        async with aiohttp.ClientSession(timeout=ERLC_TIMEOUT) as s:
            req = s.get(url, headers=ERLC_HEADERS, params=params) if method == "GET" \
                else s.post(url, headers=ERLC_HEADERS, json=payload)
            async with req as r:
                bucket = _erlc_rate_update(r.headers)
                try:
                    raw = await r.json()
                except Exception:
                    raw = None

                if r.status == 200:
                    return r.status, raw, None

                code = raw.get("code") if isinstance(raw, dict) else None
                message = raw.get("message") if isinstance(raw, dict) else None

                if r.status == 429 or code == 4001:
                    retry_after = r.headers.get("Retry-After")
                    try:
                        wait_s = float(retry_after)
                    except (TypeError, ValueError):
                        wait_s = 5.0
                    _erlc_rate_block(bucket, wait_s)
                    print(f"[ERLC RATE LIMIT] {endpoint} (bucket={bucket}); backing off {wait_s:.1f}s.")
                elif code == 3002:
                    pass  # server is genuinely offline (no players) — expected, not worth logging
                elif code in (2000, 2001, 2002, 2003, 2004):
                    print(
                        f"[ERLC AUTH ERROR] {endpoint}: code {code} — {message or 'invalid/expired key'}. "
                        "Check ERLC_SERVER_KEY / ERLC_GLOBAL_API_KEY in configs/main.py."
                    )
                elif code == 9999:
                    print(f"[ERLC ERROR] {endpoint}: in-game module is out of date (code 9999) — kick all players and retry.")
                elif code == 4000:
                    print(f"[ERLC ERROR] {endpoint}: not authorized to perform this action on this server (code 4000).")
                elif code in (4002, 4003):
                    print(f"[ERLC ERROR] {endpoint}: rejected (code {code}) — {message or 'restricted command/message'}.")
                elif code is not None:
                    print(f"[ERLC ERROR] {endpoint}: HTTP {r.status} code={code} — {message or ''}")
                elif r.status != 200:
                    print(f"[ERLC ERROR] {endpoint}: HTTP {r.status} (no error code in body)")

                return r.status, None, code
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return 0, None, None


async def erlc_get(endpoint: str, params: dict | None = None) -> dict | list | None:
    """Back-compat wrapper — just the body, discarding status/error code."""
    _status, body, _code = await _erlc_request("GET", endpoint, params=params)
    return body

async def erlc_post(endpoint: str, payload: dict) -> tuple[int, dict]:
    status, body, code = await _erlc_request("POST", endpoint, payload=payload)
    if body is not None:
        return status, body
    # On failure `_erlc_request` doesn't return the raw error body (only
    # the parsed code), so reconstruct a minimal one for callers that
    # inspect it directly, same shape ER:LC itself would send.
    return status, ({"code": code} if code is not None else {"error": "ER:LC API unreachable"})

_LAST_KNOWN_SERVER_INFO: dict = {}


def _remember_server_info(v2: dict) -> None:
    _LAST_KNOWN_SERVER_INFO.update(
        {
            "name": v2.get("Name") or v2.get("name") or _LAST_KNOWN_SERVER_INFO.get("name"),
            "max_players": v2.get("MaxPlayers") or v2.get("maxPlayers") or _LAST_KNOWN_SERVER_INFO.get("max_players"),
            "join_code": v2.get("JoinKey") or v2.get("joinKey") or v2.get("Code") or v2.get("code")
            or _LAST_KNOWN_SERVER_INFO.get("join_code"),
            "server_id": v2.get("ServerId") or v2.get("serverId") or _LAST_KNOWN_SERVER_INFO.get("server_id"),
        }
    )


async def _erlc_server_v2_with_code(**include_flags: bool) -> tuple[dict | None, int | None]:
    params = {k: "true" for k, v in include_flags.items() if v}
    _status, body, code = await _erlc_request("GET", "/v2/server", params=params)
    data = body if isinstance(body, dict) else None
    if data:
        _remember_server_info(data)
    return data, code

async def _erlc_server_v2(**include_flags: bool) -> dict | None:
    data, _code = await _erlc_server_v2_with_code(**include_flags)
    return data

async def erlc_server_info() -> dict | None:
    data, code = await _erlc_server_v2_with_code()
    if data:
        return data
    if code == 3002:
        return None  # genuinely offline — no v1 fallback needed, nothing to report
    # v1 fallback for anything else (auth hiccup, transient error, etc.)
    data_v1 = await erlc_get("/v1/server")
    return data_v1 if isinstance(data_v1, dict) else None

async def erlc_players() -> list | None:
    data, code = await _erlc_server_v2_with_code(Players=True)
    if data:
        return data.get("Players") or []
    if code == 3002:
        return []  # confirmed offline — no players, don't fall back to v1
    data_v1 = await erlc_get("/v1/server/players")
    return data_v1 if isinstance(data_v1, list) else None

async def erlc_queue() -> list | None:
    data, code = await _erlc_server_v2_with_code(Queue=True)
    if data:
        return data.get("Queue") or []
    if code == 3002:
        return []
    data_v1 = await erlc_get("/v1/server/queue")
    return data_v1 if isinstance(data_v1, list) else None

async def erlc_staff() -> list | None:
    data, code = await _erlc_server_v2_with_code(Staff=True)
    if data:
        staff_map = data.get("Staff") or {}
        names: list[str] = []
        for group in ("Admins", "Mods", "Helpers"):
            bucket = staff_map.get(group) or {}
            names.extend(bucket.values())
        return names
    if code == 3002:
        return []
    data_v1 = await erlc_get("/v1/server/staff")
    if not isinstance(data_v1, dict):
        return None
    names_v1: list[str] = []
    for group in ("Admins", "Mods"):
        bucket = data_v1.get(group) or {}
        if isinstance(bucket, dict):
            names_v1.extend(bucket.values())
    return names_v1

async def erlc_staff_roster() -> dict | None:
    """Raw {"Admins": {roblox_id_str: username}, "Mods": {...},
    "Helpers": {...}} roster of everyone with a staff rank on the server
    right now. This is the PERSISTENT assignment — it reflects rank
    whether or not that person is currently connected — unlike
    Players[].Permission, which only exists for players who happen to be
    in-game at query time.

    Returns None on ANY failure (including the server being offline/3002)
    — callers that need to distinguish "confirmed nobody has any rank"
    from "couldn't check" should treat None as "don't know, don't act."
    """
    data, _code = await _erlc_server_v2_with_code(Staff=True)
    if data:
        return data.get("Staff") or {"Admins": {}, "Mods": {}, "Helpers": {}}
    return None

async def erlc_server_snapshot() -> dict | None:
    """Return a normalized snapshot of the live ER:LC server state."""
    v2, code = await _erlc_server_v2_with_code(Players=True, Queue=True)
    if isinstance(v2, dict):
        players_payload = v2.get("Players")
        return {
            "name": v2.get("Name") or v2.get("name") or "ER:LC Private Server",
            "max_players": v2.get("MaxPlayers") or v2.get("maxPlayers") or 32,
            "join_code": v2.get("JoinKey") or v2.get("joinKey") or v2.get("Code") or v2.get("code") or "FLA",
            "server_id": v2.get("ServerId") or v2.get("serverId") or "N/A",
            "players_count": _extract_player_count(v2),
            "queue_count": _extract_queue_count(v2),
            "staff_count": _count_staff_in_game(players_payload),
        }

    if code == 3002:
        # ER:LC's own answer for "this server is legitimately offline (has
        # no players)" — trust it directly instead of falling back to the
        # deprecated v1 endpoints, which can return stale/cached data
        # (that's what was producing a ghost "68 staff, online" reading).
        return {
            "name": _LAST_KNOWN_SERVER_INFO.get("name") or "ER:LC Private Server",
            "max_players": _LAST_KNOWN_SERVER_INFO.get("max_players") or 32,
            "join_code": _LAST_KNOWN_SERVER_INFO.get("join_code") or "FLA",
            "server_id": _LAST_KNOWN_SERVER_INFO.get("server_id") or "N/A",
            "players_count": 0,
            "queue_count": 0,
            "staff_count": 0,
        }

    # Unknown v2 failure (rate limited, auth hiccup, transient error, ...)
    # — fall back to the deprecated v1 endpoints as a last resort.
    info, players, queue = await asyncio.gather(
        erlc_get("/v1/server"),
        erlc_get("/v1/server/players"),
        erlc_get("/v1/server/queue"),
    )
    if not isinstance(info, dict):
        return None

    players_list = players if isinstance(players, list) else None
    return {
        "name": info.get("Name") or info.get("name") or "ER:LC Private Server",
        "max_players": info.get("MaxPlayers") or info.get("maxPlayers") or 32,
        "join_code": info.get("JoinKey") or info.get("joinKey") or info.get("Code") or info.get("code") or "FLA",
        "server_id": info.get("ServerId") or info.get("serverId") or "N/A",
        "players_count": len(players_list) if players_list else 0,
        "queue_count": len(queue) if isinstance(queue, list) else 0,
        "staff_count": _count_staff_in_game(players_list),
    }

async def erlc_run_command(command: str) -> tuple[int, dict]:
    # Prefer v2 path, then fall back to v1 for compatibility.
    status, body = await erlc_post("/v2/server/command", {"command": command})
    if status in (404, 405):
        return await erlc_post("/v1/server/command", {"command": command})
    return status, body

# =============================================================
# UPTIME
# =============================================================

def uptime_str() -> str:
    secs = int(time.time() - START_TIME)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s"