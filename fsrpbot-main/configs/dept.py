"""
configs/dept.py — Department bots: tokens.
Use DEPT env var: apd | fcso | gsp | afrd | dhs
"""
import os

SPACER = "ㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤ"

DEPT_GUILDS = {
    "mpd":  1234567890,
    "mdso": 1234567890,
    "fhp":  1234567890,
    "mdfr": 1234567890,
    "fdot": 1234567890
}

# Department bot tokens — paste into configs/dept_tokens.py
try:
    from configs.dept_tokens import DEPT_TOKENS as _T
except ImportError:
    _T = {"mpd": "", "mdso": "", "fhp": "", "mdfr": "", "fdot": ""}
DEPT_TOKENS = {k: (str(v or "").strip() or os.environ.get(f"DEPT_{k.upper()}_TOKEN", "")) for k, v in _T.items()}
