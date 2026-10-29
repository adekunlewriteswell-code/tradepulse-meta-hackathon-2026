"""Back up and correct only the original synthetic baseline."""
import argparse
import asyncio
from pathlib import Path

from app.config import Settings
from app.database import Database
from app.services.currency_correction import backup_database, correct_synthetic_currency


async def correct(backup: Path) -> None:
    database = Database(Settings())
    try:
        if not backup.exists():
            backup_database(database, backup)
        changed = await correct_synthetic_currency(database, backup)
        print("Synthetic NGN baseline corrected." if changed else "NGN baseline already correct.")
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(correct(arguments.backup))

