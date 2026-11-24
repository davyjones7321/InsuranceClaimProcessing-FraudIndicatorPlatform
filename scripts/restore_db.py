"""Database restore automation script supporting PostgreSQL and SQLite."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings


def restore_database(backup_file: str, target_db_url: str | None = None) -> None:
    """Restore database from backup file into target database."""
    backup_path = Path(backup_file)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    target_url = target_db_url or settings.DATABASE_URL

    if "sqlite" in target_url:
        sqlite_file = target_url.split("///")[-1].split("?")[0]
        dest_path = Path(sqlite_file)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, dest_path)
        print(f"SQLite database restored successfully to: {dest_path}")
        return

    # PostgreSQL restore via pg_restore if binary exists
    pg_restore_bin = shutil.which("pg_restore")
    if pg_restore_bin and backup_path.suffix == ".dump":
        cmd = [
            pg_restore_bin,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            target_url.replace("+psycopg", ""),
            "-v",
            str(backup_path),
        ]
        print(f"Executing pg_restore into {target_url}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError(f"pg_restore failed with code {result.returncode}: {result.stderr}")
        print(f"PostgreSQL database restored successfully from: {backup_path}")
        return

    # Fallback restore using SQLAlchemy data transfer
    print(f"Restoring from backup snapshot {backup_path} into {target_url}...")
    from sqlalchemy import create_engine

    from app.db.base import Base

    src_engine = create_engine(f"sqlite:///{backup_path}")
    dst_engine = create_engine(target_url)
    Base.metadata.drop_all(bind=dst_engine)
    Base.metadata.create_all(bind=dst_engine)

    with src_engine.connect() as src_conn, dst_engine.connect() as dst_conn:
        for table in Base.metadata.sorted_tables:
            rows = src_conn.execute(table.select()).mappings().all()
            if rows:
                dst_conn.execute(table.insert(), [dict(r) for r in rows])
        dst_conn.commit()
    print(f"Database successfully restored from snapshot to {target_url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a database backup.")
    parser.add_argument("--backup", "-b", required=True, help="Path to backup file")
    parser.add_argument("--target-url", "-t", help="Target database connection URL")
    args = parser.parse_args()

    try:
        restore_database(args.backup, args.target_url)
    except Exception as e:
        print(f"Error during restore: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
