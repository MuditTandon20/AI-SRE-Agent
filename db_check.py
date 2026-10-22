import os
import psycopg
from dotenv import load_dotenv

load_dotenv()

with psycopg.connect(
    host="localhost",
    port=5433,
    dbname=os.environ["POSTGRES_DB"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, severity FROM incidents")
        for row in cur.fetchall():
            print(row)