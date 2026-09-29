-- Milestone 1: household identity, intake of immutable originals, audit,
-- consent and erasure. See docs/architecture.md.

CREATE TABLE households (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    -- Local part of the forwarding address: <inbound_token>@<inbound_domain>.
    inbound_token TEXT NOT NULL UNIQUE,
    -- The household data key, wrapped by the master key. NULL once erased:
    -- every blob of the household is unreadable from that moment.
    wrapped_key BYTEA,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'erasing')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE members (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    -- guardian: parent or legal guardian; can consent for children and manage the household.
    -- adult: another adult member (grandparent, older sibling); signs in, sees shared items.
    -- child: a data subject; does not sign in in v1.
    role TEXT NOT NULL CHECK (role IN ('guardian', 'adult', 'child')),
    email TEXT,
    date_of_birth DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (role <> 'child' OR email IS NULL)
);
CREATE UNIQUE INDEX members_household_email_idx ON members (household_id, lower(email)) WHERE email IS NOT NULL;
CREATE INDEX members_household_idx ON members (household_id);

CREATE TABLE member_tokens (
    id UUID PRIMARY KEY,
    member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

-- One row per distinct byte sequence in a household. The bytes live in the
-- object store, encrypted with the household key; they never change.
CREATE TABLE blobs (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    media_type TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (household_id, sha256)
);

-- The InputArtifact envelope: one per thing that arrived, whatever the
-- channel. An email is one artifact; each attachment is a child artifact.
CREATE TABLE input_artifacts (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    blob_id UUID NOT NULL REFERENCES blobs(id),
    parent_id UUID REFERENCES input_artifacts(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('upload', 'email', 'email_attachment')),
    submitted_by UUID REFERENCES members(id) ON DELETE SET NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('private', 'shared')),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'quarantined')),
    quarantine_reason TEXT,
    filename TEXT,
    media_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    -- Channel metadata (email: from, to, subject, message_id, auth result).
    source JSONB NOT NULL DEFAULT '{}'::jsonb,
    note TEXT,
    -- Resending the same thing returns the artifact it already made.
    dedup_key TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX input_artifacts_dedup_idx ON input_artifacts (household_id, dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX input_artifacts_household_idx ON input_artifacts (household_id, received_at DESC);
CREATE INDEX input_artifacts_blob_idx ON input_artifacts (blob_id);
CREATE INDEX input_artifacts_parent_idx ON input_artifacts (parent_id);

-- Whom an artifact is about (usually a child). Naming a child requires an
-- active consent record for that child.
CREATE TABLE artifact_subjects (
    artifact_id UUID NOT NULL REFERENCES input_artifacts(id) ON DELETE CASCADE,
    member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    PRIMARY KEY (artifact_id, member_id)
);
CREATE INDEX artifact_subjects_member_idx ON artifact_subjects (member_id);

-- Verifiable parental consent (DPDP Act, s.9): who consented, for whom, for
-- what purpose, under which notice, how it was verified, and when it ended.
CREATE TABLE consent_records (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    subject_member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    guardian_member_id UUID REFERENCES members(id) ON DELETE SET NULL,
    purpose TEXT NOT NULL,
    notice_version TEXT NOT NULL,
    verification_method TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    withdrawn_at TIMESTAMPTZ,
    withdrawn_by UUID REFERENCES members(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX consent_records_active_idx ON consent_records (subject_member_id, purpose) WHERE withdrawn_at IS NULL;

-- Append-only. No foreign keys, so audit rows survive the rows they name;
-- only an erasure may delete them, by setting familyos.audit_purge for its
-- transaction. Details never carry user content (no filenames, no text).
CREATE TABLE audit_events (
    id BIGSERIAL PRIMARY KEY,
    household_id UUID NOT NULL,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('member', 'inbound', 'system')),
    actor_member_id UUID,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id UUID,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX audit_events_household_idx ON audit_events (household_id, at DESC);

CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('familyos.audit_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_events is append-only (%)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();

-- What an erasure did, kept after everything it erased. No personal data.
CREATE TABLE erasure_log (
    id UUID PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('household', 'subject')),
    household_id UUID NOT NULL,
    subject_member_id UUID,
    job_id UUID,
    reason TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Durable jobs, from Orbit's app/jobs.py (see docs/provenance.md).
-- household_id is NULL for system jobs such as a household erasure, which
-- must outlive the household it deletes.
CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    household_id UUID REFERENCES households(id) ON DELETE CASCADE,
    member_id UUID REFERENCES members(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    plan JSONB NOT NULL DEFAULT '[]'::jsonb,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    operations JSONB NOT NULL DEFAULT '{}'::jsonb,
    signals JSONB NOT NULL DEFAULT '[]'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_id TEXT,
    lease_until TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    question TEXT,
    action_id TEXT,
    result JSONB,
    error TEXT,
    fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);
CREATE INDEX jobs_household_idx ON jobs (household_id, created_at DESC);
CREATE INDEX jobs_due_idx ON jobs (status, next_run_at, lease_until);

CREATE TABLE job_events (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX job_events_job_idx ON job_events (job_id, at);
