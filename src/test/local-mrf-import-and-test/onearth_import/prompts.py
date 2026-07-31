"""TTY-aware prompt helpers. Prompts are only appropriate when stdin is
interactive; callers should gate their use with `is_interactive()`."""

from __future__ import annotations

import sys
from dataclasses import dataclass


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@dataclass
class Choice:
    value: str
    label: str


def pick_one(prompt: str, choices: list[Choice], *, default: int | None = None) -> str:
    """Print a numbered list and return the value for the chosen index."""
    print(prompt)
    for i, c in enumerate(choices, 1):
        marker = "  (default)" if default is not None and i - 1 == default else ""
        print(f"  {i:>2}. {c.label}{marker}")
    suffix = f" [{default + 1}]" if default is not None else ""
    while True:
        raw = input(f"Pick 1-{len(choices)}{suffix}: ").strip()
        if not raw and default is not None:
            return choices[default].value
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx].value
        print(f"  (not a valid choice, pick 1-{len(choices)})")


def yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        raw = input(f"{prompt}{suffix} ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def free_text(prompt: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    if not raw and default is not None:
        return default
    return raw
