"""
utils/dept_helpers.py — Department-specific permission checks using CoC order.
"""
import discord

# Injected at runtime by dept cog setup
_DEPT_CFG = None


def set_dept_config(cfg: dict):
    global _DEPT_CFG
    _DEPT_CFG = cfg


def get_dept_config() -> dict:
    return _DEPT_CFG or {}


def _coc_role_order(guild: discord.Guild) -> dict[str, int]:
    """Map role name -> index (0 = highest). Uses existing guild roles matched by name."""
    coc = (_DEPT_CFG or {}).get("coc", [])
    from configs.dept import SPACER

    name_to_idx = {}
    for i, raw in enumerate(coc):
        name = raw if raw != "spacer" else SPACER
        name_to_idx[name] = i
    return name_to_idx


def get_member_hierarchy_index(member: discord.Member) -> int:
    """Lowest index = highest rank. Returns large number if no CoC role."""
    order = _coc_role_order(member.guild)
    best = 99999
    for r in member.roles:
        if r.name in order and order[r.name] < best:
            best = order[r.name]
    return best if best < 99999 else 99999


def actor_outranks_target(actor: discord.Member, target: discord.Member) -> bool:
    """Actor must have strictly higher rank (lower index) than target."""
    ai = get_member_hierarchy_index(actor)
    ti = get_member_hierarchy_index(target)
    return ai < ti


def is_senior_hr(member: discord.Member) -> bool:
    """SHR+ = Senior High Ranks and above (first ~15 in CoC typically)."""
    idx = get_member_hierarchy_index(member)
    # SHR is usually around index 12-14; include DO, DMG, dept head, BoC, CoS, etc.
    return idx <= 14


def is_hr(member: discord.Member) -> bool:
    """HR+ = High Ranks and above."""
    idx = get_member_hierarchy_index(member)
    return idx <= 30  # HR typically before middle ranks


def is_staff(member: discord.Member) -> bool:
    """Anyone with a CoC role above "Low Ranks" / department role."""
    coc = (_DEPT_CFG or {}).get("coc", [])
    from configs.dept import SPACER

    low_markers = ("Low Ranks", "Atlanta Police Department", "Fulton County Sheriff's Office",
                   "Georgia State Patrol", "Atlanta Fire & Rescue Department", "Department of Homeland Security")
    order = _coc_role_order(member.guild)
    low_idx = 99999
    for m in low_markers:
        if m in order:
            low_idx = min(low_idx, order[m])
    idx = get_member_hierarchy_index(member)
    return idx < low_idx


def get_suspended_role(guild: discord.Guild) -> discord.Role | None:
    return discord.utils.get(guild.roles, name="Suspended")


def get_department_role(guild: discord.Guild) -> discord.Role | None:
    """Department-wide role for mass-shift ping (e.g. Atlanta Police Department)."""
    coc = (_DEPT_CFG or {}).get("coc", [])
    dept_names = (
        "Atlanta Police Department",
        "Fulton County Sheriff's Office",
        "Georgia State Patrol",
        "Atlanta Fire & Rescue Department",
        "Department of Homeland Security",
    )
    for name in dept_names:
        if name in coc:
            return discord.utils.get(guild.roles, name=name)
    return None


def is_dept_head(member: discord.Member) -> bool:
    """True if member has the department head role (CoP, Sheriff, etc)."""
    head_names = (
        "Chief of Police", "Sheriff", "Commissioner",
        "Fire Chief", "Secretary of Homeland Security",
    )
    for r in member.roles:
        if any(h in r.name for h in head_names):
            return True
    return False


def get_role_coc_index(guild: discord.Guild, role: discord.Role) -> int:
    """CoC index for a role (0=highest). Returns 99999 if not in CoC."""
    order = _coc_role_order(guild)
    return order.get(role.name, 99999)


def dept_can_promote(actor: discord.Member, target: discord.Member, new_role: discord.Role) -> bool:
    """Actor can promote target to new_role: must outrank both; same rank can't promote except dept head."""
    if not actor_outranks_target(actor, target):
        return False
    actor_idx = get_member_hierarchy_index(actor)
    role_idx = get_role_coc_index(actor.guild, new_role)
    if actor_idx < role_idx:
        return True  # Actor outranks new role
    if actor_idx == role_idx and is_dept_head(actor):
        return True  # Same rank but dept head
    return False


def get_staff_roles_to_remove(guild: discord.Guild) -> list[discord.Role]:
    """Roles that count as staff (above Low Ranks / dept) for resign."""
    from configs.dept import SPACER

    coc = (_DEPT_CFG or {}).get("coc", [])
    low_markers = ("Low Ranks", "Atlanta Police Department", "Fulton County Sheriff's Office",
                   "Georgia State Patrol", "Atlanta Fire & Rescue Department", "Department of Homeland Security")
    order = _coc_role_order(guild)
    low_idx = 99999
    for m in low_markers:
        if m in order:
            low_idx = min(low_idx, order[m])
    roles = []
    for r in guild.roles:
        if r.name in order and order[r.name] < low_idx and r.name != SPACER:
            roles.append(r)
    return roles


def get_former_role(guild: discord.Guild) -> discord.Role | None:
    """Former/Retired role for resign."""
    for name in ("Retired Officer", "Retired Deputy", "Retired Trooper", "Retired Firefighter", "Retired Agent"):
        r = discord.utils.get(guild.roles, name=name)
        if r:
            return r
    return discord.utils.get(guild.roles, name="Former Staff")


def is_mgmt(member: discord.Member) -> bool:
    """Mgmt = Board/Dept head level (top ~10 in CoC)."""
    idx = get_member_hierarchy_index(member)
    return idx <= 10


# Aliases for department.py / department_ext
dept_is_hr = is_hr
dept_is_shr = is_senior_hr
dept_is_mgmt = is_mgmt
