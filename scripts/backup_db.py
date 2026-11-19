"""Database backup automation script supporting PostgreSQL and SQLite."""

import argparse
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings


def backup_database(
    source_db_url: str | None = None,
    output_path: str | None = None,
) -> Path:
    """Create a backup of the configured database."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    db_url = source_db_url or settings.DATABASE_URL

    if "sqlite" in db_url:
        sqlite_file = db_url.split("///")[-1].split("?")[0]
        src_path = Path(sqlite_file)
        if not src_path.exists():
            raise FileNotFoundError(f"SQLite database file not found at '{src_path}'")

        if output_path:
            dest_path = Path(output_path)
            if dest_path.is_dir():
                dest_path = dest_path / f"claims_backup_{timestamp}.sqlite"
        else:
            dest_dir = Path("backups")
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / f"claims_backup_{timestamp}.sqlite"

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        print(f"SQLite backup successfully written to: {dest_path}")
        return dest_path

    # PostgreSQL backup
    if output_path:
        dest_path = Path(output_path)
        if dest_path.is_dir():
            dest_path = dest_path / f"claims_backup_{timestamp}.dump"
    else:
        dest_dir = Path("backups")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"claims_backup_{timestamp}.dump"

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    pg_dump_bin = shutil.which("pg_dump")
    if pg_dump_bin:
        cmd = [
            pg_dump_bin,
            "--dbname",
            db_url.replace("+psycopg", ""),
            "-F",
            "c",
            "-b",
            "-v",
            "-f",
            str(dest_path),
        ]
        print(f"Executing pg_dump to {dest_path}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"pg_dump failed with code {result.returncode}: {result.stderr}")
        print(f"PostgreSQL backup successfully written to: {dest_path}")
        return dest_path

    # Fallback when pg_dump is not installed on the host
    print(
        f"Notice: pg_dump utility not found on PATH. Performing SQLite / SQL dump fallback to {dest_path}..."
    )
    from sqlalchemy import create_engine

    from app.db.base import Base

    src_engine = create_engine(db_url)
    # Dump SQLite snapshot of schema and data
    dest_sqlite = dest_path.with_suffix(".sqlite")
    dst_engine = create_engine(f"sqlite:///{dest_sqlite}")
    Base.metadata.create_all(bind=dst_engine)
    # Copy all table records
    with src_engine.connect() as src_conn, dst_engine.connect() as dst_conn:
        for table in Base.metadata.sorted_tables:
            rows = src_conn.execute(table.select()).mappings().all()
            if rows:
                dst_conn.execute(table.insert(), [dict(r) for r in rows])
        dst_conn.commit()
    print(f"Database fallback backup successfully written to: {dest_sqlite}")
    return dest_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a database backup.")
    parser.add_argument("--source-url", "-s", help="Source database connection URL")
    parser.add_argument("--output", "-o", help="Target output file or directory path")
    args = parser.parse_args()

    try:
        backup_database(source_db_url=args.source_url, output_path=args.output)
    except Exception as e:
        print(f"Error during backup: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
