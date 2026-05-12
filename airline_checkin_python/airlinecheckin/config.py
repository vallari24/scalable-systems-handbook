from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    socket: str | None


def load_config() -> DatabaseConfig:
    socket_path = os.getenv("AIRLINE_DB_SOCKET")
    if socket_path is None and Path("/tmp/mysql.sock").exists():
        socket_path = "/tmp/mysql.sock"

    return DatabaseConfig(
        host=os.getenv("AIRLINE_DB_HOST", "localhost"),
        port=int(os.getenv("AIRLINE_DB_PORT", "3306")),
        user=os.getenv("AIRLINE_DB_USER", "root"),
        password=os.getenv("AIRLINE_DB_PASSWORD", ""),
        database=os.getenv("AIRLINE_DB_NAME", "airline_checkin"),
        socket=socket_path,
    )
