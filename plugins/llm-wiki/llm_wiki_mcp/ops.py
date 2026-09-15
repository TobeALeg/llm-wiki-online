"""Operator-only backup, restore and integrity checks for the shared database."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class OperationsError(RuntimeError):
    pass


@contextmanager
def _database(path: Path):
    connection = sqlite3.connect(path)
    try:
        yield connection
    finally:
        connection.close()


def _check_sqlite(path: Path) -> None:
    if not path.is_file():
        raise OperationsError(f"Database does not exist: {path}")
    with _database(path) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise OperationsError("SQLite integrity check failed.")


def backup_database(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    _check_sqlite(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with _database(source_path) as source_db, _database(temporary) as destination_db:
        source_db.backup(destination_db)
    _check_sqlite(temporary)
    temporary.replace(destination_path)
    return destination_path


def restore_database(backup: str | Path, destination: str | Path) -> Path:
    backup_path = Path(backup).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    _check_sqlite(backup_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_suffix(destination_path.suffix + ".restore.tmp")
    if temporary.exists():
        temporary.unlink()
    with _database(backup_path) as backup_db, _database(temporary) as destination_db:
        backup_db.backup(destination_db)
    _check_sqlite(temporary)
    temporary.replace(destination_path)
    return destination_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Back up or restore an LLM Wiki database.")
    sub = result.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("source")
    backup.add_argument("destination")
    restore = sub.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("destination")
    integrity = sub.add_parser("integrity")
    integrity.add_argument("database")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "backup":
            path = backup_database(args.source, args.destination)
        elif args.command == "restore":
            path = restore_database(args.backup, args.destination)
        else:
            _check_sqlite(Path(args.database).expanduser().resolve())
            path = Path(args.database)
    except OperationsError as exc:
        print(f"error: {exc}")
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
