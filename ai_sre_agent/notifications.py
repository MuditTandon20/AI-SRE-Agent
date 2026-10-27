"""Durable delivery attempts. No external notification is sent unless configured."""

import argparse
import logging
from uuid import uuid4

from psycopg.rows import dict_row

from ai_sre_agent.connectors import BoundedHTTP, ConnectorError
from ai_sre_agent.domain import Report
from ai_sre_agent.safety import configured_url, redact

logger = logging.getLogger("ai_sre_agent.notifications")


class SlackMCP:
    def __init__(self, settings, http=None):
        self.settings = settings
        self.http = http or BoundedHTTP(
            settings.connector_timeout_seconds, settings.max_response_bytes
        )

    def send(self, destination, text, message_id):
        if destination != self.settings.slack_channel or not self.settings.slack_mcp_url:
            raise ConnectorError("Slack destination or MCP server is not configured")
        url = configured_url(self.settings.slack_mcp_url)
        headers = {"Accept": "application/json, text/event-stream"}
        if self.settings.slack_mcp_token:
            headers["Authorization"] = "Bearer " + self.settings.slack_mcp_token.get_secret_value()
        response = self.http.json(
            "POST",
            url,
            headers=headers,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "ai-sre-agent", "version": "0.1.0"},
                },
            },
        )
        if response.get("error"):
            raise ConnectorError("MCP initialization rejected")
        session = self.http.response_headers.get("mcp-session-id")
        if session:
            headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = response.get("result", {}).get(
            "protocolVersion", "2025-03-26"
        )
        self.http.json(
            "POST",
            url,
            headers=headers,
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        result = self.http.json(
            "POST",
            url,
            headers=headers,
            body={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": self.settings.slack_tool_name,
                    "arguments": {"channel": destination, "text": text[:10000]},
                },
            },
        )
        if result.get("error") or result.get("result", {}).get("isError"):
            raise ConnectorError("Slack MCP tool rejected delivery")
        # MCP tool schemas differ; retain correlation ID, not a guessed Slack ts.
        return "mcp-accepted:" + str(message_id)


class SES:
    def __init__(self, settings, client=None):
        self.settings = settings
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "sesv2",
                region_name=settings.ses_region,
                config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 0}),
            )
        self.client = client

    def send(self, destination, text, message_id):
        if destination != self.settings.ses_recipient or not self.settings.ses_sender:
            raise ConnectorError("SES sender/destination is not configured")
        result = self.client.send_email(
            FromEmailAddress=self.settings.ses_sender,
            Destination={"ToAddresses": [destination]},
            Content={
                "Simple": {
                    "Subject": {"Data": "AI SRE incident investigation", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": text[:10000], "Charset": "UTF-8"}},
                }
            },
            EmailTags=[{"Name": "investigation_notification", "Value": str(message_id)}],
        )
        return result["MessageId"]


def dispatch_once(store, settings, sender=None):
    token = uuid4()
    with store.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "UPDATE notification_outbox SET state='failed',error='attempts_exhausted' WHERE state='sending' AND lease_until<now() AND attempt>=3"
        )
        item = cur.execute(
            "WITH c AS (SELECT id FROM notification_outbox WHERE "
            "((state='pending' AND next_attempt<=now()) OR (state='sending' AND lease_until<now())) "
            "AND attempt<3 ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE notification_outbox n SET state='sending',attempt=n.attempt+1,lease_token=%s,"
            "lease_until=now()+interval '60 seconds' FROM c WHERE n.id=c.id RETURNING n.*",
            (token,),
        ).fetchone()
    if not item:
        return False
    try:
        report = Report.model_validate(store.report(item["investigation_id"]))
        # Summaries only: raw evidence/logs are excluded from notification channels.
        text = redact(
            f"Incident {report.incident_id} [{report.mode}]\n{report.service}/{report.environment}\n"
            f"Outcome: {report.analysis.outcome}\n{report.analysis.summary}\n"
            f"Review report via /investigations/{report.investigation_id}/report. No actions executed."
        )
        if item["mode"] == "fixture":
            provider_id = "fixture:" + str(item["id"])
        elif settings.notification_mode != "live":
            raise ConnectorError("Live delivery disabled")
        elif sender:
            provider_id = sender.send(item["destination"], text, item["id"])
        elif item["channel"] == "slack":
            provider_id = SlackMCP(settings).send(item["destination"], text, item["id"])
        elif item["channel"] == "ses":
            provider_id = SES(settings).send(item["destination"], text, item["id"])
        else:
            raise ConnectorError("Unsupported notification channel")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE notification_outbox SET state='accepted',provider_id=%s,lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s",
                (provider_id, item["id"], token),
            )
    except Exception as exc:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE notification_outbox SET state=%s,error=%s,lease_token=NULL,lease_until=NULL,"
                "next_attempt=now()+(%s * interval '1 second') WHERE id=%s AND lease_token=%s",
                (
                    "failed" if item["attempt"] >= 3 else "pending",
                    type(exc).__name__,
                    2 ** item["attempt"],
                    item["id"],
                    token,
                ),
            )
        logger.warning("notification_failed id=%s error_type=%s", item["id"], type(exc).__name__)
    return True


def main():
    from ai_sre_agent.config import Settings
    from ai_sre_agent.database import create_pool
    from ai_sre_agent.store import Store

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.parse_args()
    settings = Settings.from_env()
    with create_pool(settings) as pool:
        pool.wait(settings.database_startup_timeout_seconds)
        dispatch_once(Store(pool), settings)


if __name__ == "__main__":
    main()
