from __future__ import annotations

from contextlib import contextmanager

import mysql.connector

from .config import DatabaseConfig


def connect(config: DatabaseConfig):
    if config.socket:
        return mysql.connector.connect(
            unix_socket=config.socket,
            user=config.user,
            password=config.password,
            database=config.database,
        )

    return mysql.connector.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
    )


@contextmanager
def connection(config: DatabaseConfig):
    conn = connect(config)
    try:
        yield conn
    finally:
        conn.close()
