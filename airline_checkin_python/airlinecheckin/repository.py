from __future__ import annotations

from typing import Sequence

from .models import Seat, Trip, User


class AirlineRepository:
    def __init__(self, connection):
        self._connection = connection

    def list_trips(self) -> list[Trip]:
        cursor = self._connection.cursor(dictionary=True)
        try:
            cursor.execute("SELECT ID, name FROM trips ORDER BY ID")
            rows = cursor.fetchall()
            return [Trip(id=row["ID"], name=row["name"]) for row in rows]
        finally:
            cursor.close()

    def list_users(self, limit: int = 10) -> list[User]:
        cursor = self._connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT ID, name FROM users ORDER BY ID LIMIT %s",
                (limit,),
            )
            rows = cursor.fetchall()
            return [User(id=row["ID"], name=row["name"]) for row in rows]
        finally:
            cursor.close()

    def list_seats(self, trip_id: int, limit: int = 10) -> list[Seat]:
        cursor = self._connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT ID, NAME, TRIP_ID, USER_ID
                FROM seats
                WHERE TRIP_ID = %s
                ORDER BY ID
                LIMIT %s
                """.strip(),
                (trip_id, limit),
            )
            rows = cursor.fetchall()
            return [
                Seat(
                    id=row["ID"],
                    name=row["NAME"],
                    trip_id=row["TRIP_ID"],
                    user_id=row["USER_ID"],
                )
                for row in rows
            ]
        finally:
            cursor.close()

    def list_available_seats(self, trip_id: int, limit: int = 10) -> list[Seat]:
        cursor = self._connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT ID, NAME, TRIP_ID, USER_ID
                FROM seats
                WHERE TRIP_ID = %s AND USER_ID IS NULL
                ORDER BY ID
                LIMIT %s
                """.strip(),
                (trip_id, limit),
            )
            rows = cursor.fetchall()
            return [
                Seat(
                    id=row["ID"],
                    name=row["NAME"],
                    trip_id=row["TRIP_ID"],
                    user_id=row["USER_ID"],
                )
                for row in rows
            ]
        finally:
            cursor.close()

    def fetch_next_available_seat(
        self,
        trip_id: int,
        lock_clause: str = "",
    ) -> Seat | None:
        query = """
            SELECT ID, NAME, TRIP_ID, USER_ID
            FROM seats
            WHERE TRIP_ID = %s AND USER_ID IS NULL
            ORDER BY ID
            LIMIT 1
        """.strip()
        if lock_clause:
            query = f"{query} {lock_clause}"

        cursor = self._connection.cursor(dictionary=True)
        try:
            cursor.execute(query, (trip_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return Seat(
                id=row["ID"],
                name=row["NAME"],
                trip_id=row["TRIP_ID"],
                user_id=row["USER_ID"],
            )
        finally:
            cursor.close()

    def update_seat_user(self, seat_id: int, user_id: int) -> None:
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                "UPDATE seats SET USER_ID = %s WHERE ID = %s",
                (user_id, seat_id),
            )
        finally:
            cursor.close()

    def claim_seat(self, seat_id: int, user_id: int) -> bool:
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE seats
                SET USER_ID = %s
                WHERE ID = %s
                  AND USER_ID IS NULL
                """.strip(),
                (user_id, seat_id),
            )
            return cursor.rowcount == 1
        finally:
            cursor.close()

    def execute(self, query: str, params: Sequence[object] | None = None) -> None:
        cursor = self._connection.cursor()
        try:
            cursor.execute(query, params or ())
        finally:
            cursor.close()
