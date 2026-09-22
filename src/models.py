"""Shared data models for NopeList. Every module builds against these."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Person:
    """A blacklisted (or safe-listed) person stored in the Cognee brain."""
    name: str
    reason: str
    aliases: list[str] = field(default_factory=list)
    instagram: Optional[str] = None  # handle without '@'
    x_handle: Optional[str] = None   # handle without '@'
    threat_weight: int = 2           # 1 = awkward, 2 = avoid, 3 = evacuate
    safe: bool = False               # True = someone you WANT to see


@dataclass
class Guest:
    """A guest scraped from a Luma event guest list."""
    name: str
    instagram: Optional[str] = None
    x_handle: Optional[str] = None


@dataclass
class Event:
    url: str
    title: str
    guests: list[Guest] = field(default_factory=list)
    guest_count: Optional[int] = None  # Luma's displayed total (hidden profiles excluded)
    from_cache: bool = False


@dataclass
class Match:
    """Result of resolving a scraped Guest against the brain."""
    person: Person
    guest: Guest
    confidence: float          # 0.0 - 1.0
    matched_on: str            # 'instagram' | 'x_handle' | 'name' | 'alias' | 'graph'


@dataclass
class EventResult:
    event: Event
    hits: list[Match] = field(default_factory=list)
    safe_hits: list[Match] = field(default_factory=list)

    @property
    def threat_score(self) -> int:
        return sum(m.person.threat_weight for m in self.hits)

    @property
    def threat_level(self) -> str:
        s = self.threat_score
        if s == 0:
            return "CLEAR"
        if s <= 2:
            return "AWKWARD"
        return "EVACUATE"


def to_dict(obj) -> dict:
    return asdict(obj)
