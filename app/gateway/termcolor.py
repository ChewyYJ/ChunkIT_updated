"""ChunkIT-branded terminal output: bright green on dark, ANSI truecolor.

Colors are disabled automatically when stdout is not a TTY or NO_COLOR is set,
so piped logs and CI output stay clean.
"""

from __future__ import annotations

import os
import sys

if os.name == "nt":  # enable VT processing in legacy Windows consoles
    os.system("")

_ENABLED = sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _can_encode(char: str) -> bool:
    try:
        char.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _print(text: str) -> None:
    """Print without ever crashing on a console codepage (e.g. cp1252)."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding))

GREEN = "\x1b[38;2;34;197;94m" if _ENABLED else ""       # ChunkIT brand green
BRIGHT_GREEN = "\x1b[38;2;74;222;128m" if _ENABLED else ""
WHITE = "\x1b[38;2;245;245;245m" if _ENABLED else ""
RED = "\x1b[38;2;248;113;113m" if _ENABLED else ""
YELLOW = "\x1b[38;2;250;204;21m" if _ENABLED else ""
DIM = "\x1b[2m" if _ENABLED else ""
BOLD = "\x1b[1m" if _ENABLED else ""
RESET = "\x1b[0m" if _ENABLED else ""


def log(run_id: str | None, tag: str, message: object) -> None:
    prefix = f"{GREEN}{BOLD}[chunkit]{RESET}"
    run = f"{DIM}[run={run_id}]{RESET} " if run_id else ""
    _print(f"{prefix} {run}{BRIGHT_GREEN}[{tag}]{RESET} {message}")


def warn(run_id: str | None, tag: str, message: object) -> None:
    prefix = f"{GREEN}{BOLD}[chunkit]{RESET}"
    run = f"{DIM}[run={run_id}]{RESET} " if run_id else ""
    _print(f"{prefix} {run}{YELLOW}[{tag}]{RESET} {message}")


def error(run_id: str | None, tag: str, message: object) -> None:
    prefix = f"{GREEN}{BOLD}[chunkit]{RESET}"
    run = f"{DIM}[run={run_id}]{RESET} " if run_id else ""
    _print(f"{prefix} {run}{RED}[{tag}]{RESET} {message}")


def banner() -> str:
    block = "■" if _can_encode("■") else "#"
    dash = "—" if _can_encode("—") else "-"
    blocks = f"{GREEN}{block} {block} {block}{RESET}"
    name = f"{GREEN}{BOLD}CHUNK{RESET}{WHITE}{BOLD}IT{RESET}"
    return f"  {blocks}  {name} {DIM}{dash} context-compression gateway{RESET}"
