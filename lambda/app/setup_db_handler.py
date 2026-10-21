"""Run once after `terraform apply`: tables, row-level security and demo data in Aurora.

    aws lambda invoke --function-name northwind-setup-db response.json

Safe to run twice: every statement in db/*.sql is written to be repeatable.
"""

from pathlib import Path
import os
import psycopg
from psycopg import sql

from app import config, database, secrets

DB_DIR = Path(__file__).parent.parent / "db"
SCRIPTS = ["01_schema.sql", "02_security.sql", "03_seed_data.sql"]


def delete_conversations(event):
    """Remove test chats so people signing in see an empty history. Messages go with them (ON DELETE CASCADE).

    The app's database user can only read and add chat history, so deleting needs this admin function.
    """
    if event.get("delete_all_conversations") is True:
        rows = database.run_admin_query("DELETE FROM conversations RETURNING id")
    else:
        ids = [int(value) for value in event["delete_conversations"]]
        # A request that fails or times out leaves a conversation with no messages. After 30 seconds no request
        # can still be filling it, because API Gateway ends every request at 29 seconds.
        rows = database.run_admin_query(
            """
            DELETE FROM conversations c
            WHERE c.id = ANY(%s)
               OR (c.created_at < now() - interval '30 seconds'
                   AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id))
            RETURNING id
            """,
            (ids,),
        )
    print(f"deleted {len(rows)} conversations")
    return {"deleted_conversations": len(rows)}


def handler(event, context):
    if "delete_conversations" in event or "delete_all_conversations" in event:
        return delete_conversations(event)

    # Safe to run twice: if the tables already exist, change nothing
    rows = database.run_admin_query("SELECT to_regclass('public.employees') IS NOT NULL AS ready")
    exists = rows[0]["ready"]

    if exists:
        print("tables already exist, skipping the SQL scripts")
    else:
        for name in SCRIPTS:
            database.run_admin_script((DB_DIR / name).read_text(encoding="utf-8"))
            print(f"ran {name}")

    # Test accounts are upserted on every run, so databases created before they existed get them too.
    database.run_admin_script((DB_DIR / "04_test_accounts.sql").read_text(encoding="utf-8"))
    print("applied 04_test_accounts.sql")

    # The sign-in profile includes the person's location: also on databases created earlier.
    database.run_admin_script((DB_DIR / "05_profile_location.sql").read_text(encoding="utf-8"))
    print("applied 05_profile_location.sql")

    # The app user's real password lives in Secrets Manager, never in a SQL file
    app_password = secrets.read_secret(config.DB_APP_SECRET_ARN)["password"]
    database.set_app_user_password(app_password)
    print("set the hr_app password from Secrets Manager")

    gateway = secrets.read_secret(os.environ['GATEWAY_SETTINGS_SECRET_ARN'])
    with psycopg.connect(database.connection_url(admin=True), autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname='llm_gateway'").fetchone():
            connection.execute('CREATE ROLE llm_gateway LOGIN')
        connection.execute(sql.SQL('ALTER ROLE llm_gateway WITH PASSWORD {}').format(sql.Literal(gateway['password'])))
        connection.execute(sql.SQL('GRANT llm_gateway TO {}').format(sql.Identifier(connection.info.user)))
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname='litellm'").fetchone():
            connection.execute('CREATE DATABASE litellm OWNER llm_gateway')
        connection.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(config.DB_NAME)))
        connection.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO hr_app').format(sql.Identifier(config.DB_NAME)))
        # Added after the first release: databases created earlier skip 02_security.sql, so grant it here too.
        connection.execute('GRANT DELETE ON conversations TO hr_app')
    print('Initialized separate LiteLLM database and restricted database role')

    employees = database.run_admin_query("SELECT count(*) AS n FROM employees")[0]["n"]
    print(f"{employees} employees in the database")
    return {"employees": employees, "scripts_ran": not exists}
