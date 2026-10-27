"""Explicit retention command; previews by default."""

import argparse

from ai_sre_agent.config import Settings
from ai_sre_agent.database import create_pool
from ai_sre_agent.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete expired resolved incidents and cascaded investigation data",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    if not args.apply:
        print(
            f"Preview only: retention is {settings.retention_days} days; only resolved incidents without remediation proposals are eligible. Pass --apply after reviewing backups."
        )
        return
    with create_pool(settings) as pool:
        pool.wait(settings.database_startup_timeout_seconds)
        print(
            f"Removed {Store(pool).retention(settings.retention_days)} expired resolved incidents."
        )


if __name__ == "__main__":
    main()
