"""Print stored incident summaries using the same configuration as the API."""

import psycopg

from ai_sre_agent.config import Settings


def main():
    settings = Settings.from_env()
    with psycopg.connect(**settings.connection_kwargs()) as conn:
        for row in conn.execute(
            "SELECT id, title, severity FROM incidents ORDER BY id DESC LIMIT 100"
        ):
            print(row)


if __name__ == "__main__":
    main()
