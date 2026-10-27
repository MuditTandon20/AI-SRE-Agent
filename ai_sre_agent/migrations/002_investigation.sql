ALTER TABLE incidents ADD COLUMN IF NOT EXISTS service TEXT NOT NULL DEFAULT 'demo-service';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'local';
CREATE TABLE alert_occurrences (
    occurrence_key TEXT PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents(id),
    fingerprint TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('firing','resolved')),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE investigations (
    id UUID PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed','cancelled')),
    mode TEXT NOT NULL CHECK (mode IN ('fixture','live')),
    scenario TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    budget JSONB NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE UNIQUE INDEX one_active_investigation ON investigations(incident_id) WHERE status IN ('queued','running');
CREATE INDEX investigation_queue ON investigations(next_attempt,created_at) WHERE status IN ('queued','running');
CREATE TABLE timeline_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id BIGINT REFERENCES incidents(id) ON DELETE CASCADE,
    investigation_id UUID REFERENCES investigations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX timeline_incident ON timeline_events(incident_id,id);
CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    investigation_id UUID NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    body JSONB NOT NULL
);
CREATE TABLE workflow_checkpoints (
    investigation_id UUID NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    body JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(investigation_id,stage)
);
CREATE TABLE reports (
    investigation_id UUID PRIMARY KEY REFERENCES investigations(id) ON DELETE CASCADE,
    incident_id BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE notification_outbox (
    id UUID PRIMARY KEY,
    investigation_id UUID NOT NULL REFERENCES reports(investigation_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    destination TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','sending','accepted','delivered','failed')),
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider_id TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(investigation_id,channel,destination)
);
CREATE TABLE remediation_proposals (
    id UUID PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents(id),
    body JSONB NOT NULL,
    digest TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed',
    expires_at TIMESTAMPTZ NOT NULL,
    approved_digest TEXT,
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    error TEXT,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE historical_documents (
    id UUID PRIMARY KEY,
    incident_id BIGINT REFERENCES incidents(id) ON DELETE CASCADE,
    service TEXT NOT NULL,
    environment TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX history_scope ON historical_documents(service,environment);
