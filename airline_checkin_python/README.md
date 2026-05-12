# Airline Check-In Python Scaffold

This folder is a Python rewrite of the airline check-in example.

It is intentionally lightweight:

- shared MySQL connection and row-loading helpers
- four runnable approach entrypoints
- no booking logic yet

## Layout

```text
airline_checkin_python/
  airlinecheckin/
    config.py
    db.py
    models.py
    repository.py
  approach1/main.py
  approach2/main.py
  approach3/main.py
  approach4/main.py
  requirements.txt
```

## Database

The schema matches the dump in `/Users/vallari/Downloads/airline.txt`:

- `trips`
- `seats`
- `users`

## Environment

Set these variables before running:

```text
AIRLINE_DB_HOST=localhost
AIRLINE_DB_PORT=3306
AIRLINE_DB_USER=root
AIRLINE_DB_PASSWORD=
AIRLINE_DB_NAME=airline_checkin
```

If you are using the dump as-is, set `AIRLINE_DB_NAME=tbs`.

## Run

From this folder, run one of the approaches:

```bash
python approach1/main.py
python approach2/main.py
python approach3/main.py
python approach4/main.py
```

Each entrypoint only loads rows and prints a small preview. You can fill in the booking logic later.
