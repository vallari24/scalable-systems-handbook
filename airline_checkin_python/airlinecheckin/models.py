from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Trip:
    id: int
    name: str


@dataclass(frozen=True)
class User:
    id: int
    name: str


@dataclass(frozen=True)
class Seat:
    id: int
    name: str
    trip_id: int
    user_id: int | None
