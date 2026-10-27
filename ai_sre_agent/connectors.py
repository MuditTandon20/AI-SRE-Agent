"""Reviewed read-only connectors with fixed operations and bounded responses."""

import json
import time
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

import httpx

from ai_sre_agent.domain import Evidence, utcnow
from ai_sre_agent.safety import configured_url, redact
from ai_sre_agent.scenarios import scenario_signal


class ConnectorError(RuntimeError):
    pass


class BoundedHTTP:
    def __init__(self, timeout=10, limit=65536, transport=None, verify=True):
        self.timeout = timeout
        self.limit = limit
        self.transport = transport
        self.verify = verify
        self.response_headers = {}

    def json(self, method, url, *, params=None, headers=None, body=None):
        deadline = time.monotonic() + self.timeout
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
                verify=self.verify,
            ) as client:
                with client.stream(
                    method, url, params=params, headers=headers, json=body
                ) as response:
                    self.response_headers = dict(response.headers)
                    if response.status_code >= 300:
                        raise ConnectorError(f"HTTP {response.status_code}")
                    result = bytearray()
                    for chunk in response.iter_bytes(chunk_size=4096):
                        if time.monotonic() > deadline:
                            raise ConnectorError("Response deadline exceeded")
                        result.extend(chunk)
                        if len(result) > self.limit:
                            raise ConnectorError("Response exceeds configured size limit")
            if not result:
                return {}
            if "text/event-stream" in self.response_headers.get("content-type", ""):
                events = [
                    json.loads(line[5:].strip())
                    for line in result.decode().splitlines()
                    if line.startswith("data:")
                ]
                matching = [event for event in events if event.get("id") == (body or {}).get("id")]
                if not matching:
                    raise ConnectorError("MCP returned no matching response")
                return matching[-1]
            return json.loads(result)
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError(type(exc).__name__) from None


class Connectors:
    capabilities = {
        "prometheus": {
            "version": "1",
            "permissions": ["metrics:read"],
            "operations": ["service_metrics"],
        },
        "loki": {"version": "1", "permissions": ["logs:read"], "operations": ["service_logs"]},
        "tempo": {"version": "1", "permissions": ["traces:read"], "operations": ["service_traces"]},
        "github": {
            "version": "1",
            "permissions": ["contents:read"],
            "operations": ["recent_commits"],
        },
        "runbooks": {
            "version": "1",
            "permissions": ["files:read:configured-directory"],
            "operations": ["retrieve"],
        },
        "kubernetes": {
            "version": "1",
            "permissions": ["pods:read:configured-namespace"],
            "operations": ["pod_status"],
        },
    }

    def __init__(self, settings, http=None):
        self.settings = settings
        self.http = http or BoundedHTTP(
            settings.connector_timeout_seconds, settings.max_response_bytes
        )

    def _evidence(self, source, kind, query, incident, content, url=None, observed=None):
        text = redact(content if isinstance(content, str) else json.dumps(content, default=str))
        limit = self.settings.max_response_bytes // 2
        return Evidence(
            source=source,
            kind=kind,
            query=query,
            resource=incident["service"],
            observed_at=observed or utcnow(),
            content=text[:limit],
            source_url=url,
            truncated=len(text) > limit,
        )

    def fixture(self, incident, scenario):
        signal = scenario_signal(scenario or "ambiguous")
        return [
            Evidence(
                source="fixture:logs",
                kind="logs",
                observed_at=utcnow(),
                query="synthetic scenario replay",
                resource=incident["service"],
                content=signal,
                synthetic=True,
            )
        ]

    def prometheus(self, incident):
        base = configured_url(self.settings.prometheus_url)
        service = json.dumps(incident["service"])
        query = f"sum by (code) (rate(demo_requests_total{{service={service}}}[1m]))"
        now = utcnow()
        data = self.http.json(
            "GET",
            base + "/api/v1/query_range",
            params={
                "query": query,
                "start": (now - timedelta(minutes=self.settings.max_query_minutes)).timestamp(),
                "end": now.timestamp(),
                "step": 15,
            },
        )
        if data.get("status") != "success":
            raise ConnectorError("Prometheus query failed")
        return [
            self._evidence(
                "prometheus", "metrics", query, incident, data.get("data", {}), base + "/graph"
            )
        ]

    def loki(self, incident):
        base = configured_url(self.settings.loki_url)
        query = (
            "{service="
            + json.dumps(incident["service"])
            + ",environment="
            + json.dumps(incident["environment"])
            + "}"
        )
        now = utcnow()
        data = self.http.json(
            "GET",
            base + "/loki/api/v1/query_range",
            params={
                "query": query,
                "start": str(
                    int(
                        (now - timedelta(minutes=self.settings.max_query_minutes)).timestamp() * 1e9
                    )
                ),
                "end": str(int(now.timestamp() * 1e9)),
                "limit": 100,
                "direction": "backward",
            },
        )
        if data.get("status") != "success":
            raise ConnectorError("Loki query failed")
        rows = data.get("data", {}).get("result", [])
        result = []
        for stream in rows[:20]:
            for stamp, line in stream.get("values", [])[:100]:
                from datetime import datetime, timezone

                observed = datetime.fromtimestamp(int(stamp) / 1e9, timezone.utc)
                result.append(self._evidence("loki", "logs", query, incident, line, base, observed))
                if len(result) >= 100:
                    return result
        return result

    def tempo(self, incident):
        base = configured_url(self.settings.tempo_url)
        query = "{ resource.service.name = " + json.dumps(incident["service"]) + " }"
        now = utcnow()
        data = self.http.json(
            "GET",
            base + "/api/search",
            params={
                "q": query,
                "limit": 5,
                "start": int(
                    (now - timedelta(minutes=self.settings.max_query_minutes)).timestamp()
                ),
                "end": int(now.timestamp()),
            },
        )
        evidence = [self._evidence("tempo", "traces", query, incident, data, base)]
        for trace in data.get("traces", [])[:2]:
            trace_id = trace.get("traceID", "")
            if not 16 <= len(trace_id) <= 32 or any(
                c not in "0123456789abcdefABCDEF" for c in trace_id
            ):
                continue
            trace_url = base + "/api/traces/" + trace_id
            body = self.http.json("GET", trace_url, headers={"Accept": "application/json"})
            evidence.append(
                self._evidence("tempo", "traces", "trace_id=" + trace_id, incident, body, trace_url)
            )
        return evidence

    def github(self, incident):
        repo = self.settings.github_repository
        if (
            not repo
            or len(repo.split("/")) != 2
            or any(
                not part.replace("-", "").replace("_", "").replace(".", "").isalnum()
                for part in repo.split("/")
            )
        ):
            raise ConnectorError("Configure GitHub as owner/repository")
        url = "https://api.github.com/repos/" + repo + "/commits"
        headers = {"Accept": "application/vnd.github+json"}
        if self.settings.github_token:
            headers["Authorization"] = "Bearer " + self.settings.github_token.get_secret_value()
        data = self.http.json("GET", url, params={"per_page": 5}, headers=headers)
        # Time correlation alone cannot establish causation.
        return [
            self._evidence(
                "github",
                "change",
                "recent commits (correlation only)",
                incident,
                [
                    {"sha": x["sha"], "message": x["commit"]["message"], "url": x["html_url"]}
                    for x in data
                ],
                "https://github.com/" + repo,
            )
        ]

    def kubernetes(self, incident):
        base = configured_url(self.settings.kubernetes_url)
        namespace = quote(self.settings.kubernetes_namespace, safe="")
        headers = {}
        if self.settings.kubernetes_token:
            headers["Authorization"] = "Bearer " + self.settings.kubernetes_token.get_secret_value()
        client = BoundedHTTP(
            self.http.timeout,
            self.settings.max_response_bytes,
            transport=self.http.transport,
            verify=self.settings.kubernetes_ca_file or True,
        )
        query = "app=" + incident["service"]
        data = client.json(
            "GET",
            base + f"/api/v1/namespaces/{namespace}/pods",
            params={"labelSelector": query, "limit": 20},
            headers=headers,
        )
        normalized = [
            {
                "name": pod.get("metadata", {}).get("name"),
                "phase": pod.get("status", {}).get("phase"),
                "containers": pod.get("status", {}).get("containerStatuses", []),
            }
            for pod in data.get("items", [])[:20]
        ]
        return [self._evidence("kubernetes", "infrastructure", query, incident, normalized, base)]

    def runbooks(self, incident):
        root = Path(
            self.settings.runbooks_path or str(files("ai_sre_agent").joinpath("runbooks"))
        ).resolve()
        if not root.is_dir():
            return []
        terms = set((incident["title"] + " " + incident["symptom"]).lower().split())
        candidates = []
        for path in list(root.glob("*.md"))[:100]:
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(root)
                or resolved.stat().st_size > self.settings.max_response_bytes
            ):
                continue
            content = resolved.read_text(encoding="utf-8")
            score = len(terms & set(content.lower().split()))
            if score:
                candidates.append((score, path.name, content))
        return [
            self._evidence("runbook:" + name, "runbook", "keyword retrieval", incident, text)
            for _, name, text in sorted(candidates, reverse=True)[:3]
        ]

    def health(self, name):
        base, path = {
            "prometheus": (self.settings.prometheus_url, "/-/ready"),
            "loki": (self.settings.loki_url, "/ready"),
            "tempo": (self.settings.tempo_url, "/ready"),
        }.get(name, (None, None))
        if base is None:
            raise ConnectorError("Connector not configured")
        # These readiness endpoints return text rather than JSON.
        try:
            with httpx.Client(
                timeout=self.settings.connector_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.get(configured_url(base) + path)
                return response.status_code == 200
        except httpx.HTTPError:
            return False
