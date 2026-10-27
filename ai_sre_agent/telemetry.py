"""Opt-in trace export and database-derived workflow metrics."""

from prometheus_client.core import GaugeMetricFamily


def configure_tracing(settings):
    if not settings.otel_enabled:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "ai-sre-agent"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


class WorkflowCollector:
    def __init__(self, app):
        self.app = app

    def collect(self):
        import psycopg
        from psycopg_pool import PoolClosed, PoolTimeout

        database = GaugeMetricFamily(
            "ai_sre_database_up", "Database accessible for workflow metrics"
        )
        queue = GaugeMetricFamily(
            "ai_sre_investigations", "Persisted investigation counts", labels=["status"]
        )
        calls = GaugeMetricFamily(
            "ai_sre_reserved_calls",
            "Reserved calls retained in investigation history",
            labels=["kind"],
        )
        try:
            with self.app.state.pool.connection() as conn:
                for status, count in conn.execute(
                    "SELECT status,count(*) FROM investigations GROUP BY status"
                ):
                    queue.add_metric([status], count)
                row = conn.execute(
                    "SELECT COALESCE(sum((budget->>'tool_calls')::int),0),COALESCE(sum((budget->>'model_calls')::int),0) FROM investigations"
                ).fetchone()
                calls.add_metric(["tool"], row[0])
                calls.add_metric(["model"], row[1])
            database.add_metric([], 1)
        except (psycopg.Error, PoolClosed, PoolTimeout, AttributeError):
            database.add_metric([], 0)
        yield database
        yield queue
        yield calls
