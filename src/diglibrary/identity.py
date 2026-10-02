"""The portable identifier format shared by every extensible DigLibrary registry."""

import re

IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
"""Lowercase snake-case names that survive configuration files, paths, and storage."""


def validate_identifier(value: str, kind: str) -> None:
    """Reject an identifier that would not be portable across configuration and storage.

    Every extensible DigLibrary registry — providers, metadata sources,
    capabilities — validates identity through this one function instead of a
    closed enumeration, so a new member never requires a change to the core.
    """
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {kind} identifier: {value!r}")
