-- Milestone 2: extraction. Claims read from an artifact, each pointing at
-- where it came from in the original, and the obligations proposed from
-- them. Everything cascades with its artifact, so erasing an artifact (or a
-- child's data, or the household) erases what was read from it.

-- One run of an extractor over one artifact. A re-run supersedes the last.
CREATE TABLE extractions (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    artifact_id UUID NOT NULL REFERENCES input_artifacts(id) ON DELETE CASCADE,
    job_id UUID,
    extractor TEXT NOT NULL,            -- claude | rules
    model TEXT,                         -- the model that answered, when one did
    prompt_version TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    reference_date DATE,                -- "today" for relative dates
    actionable BOOLEAN NOT NULL,
    non_actionable_reason TEXT,
    page_count INTEGER NOT NULL,
    ocr_pages INTEGER NOT NULL DEFAULT 0,
    notes JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX extractions_current_idx ON extractions (artifact_id) WHERE superseded_at IS NULL;
CREATE INDEX extractions_household_idx ON extractions (household_id, created_at DESC);

-- One fact the notice states, with the words it was read from and where
-- they are: page, character span in the page text, and a box per line.
CREATE TABLE claims (
    id UUID PRIMARY KEY,
    extraction_id UUID NOT NULL REFERENCES extractions(id) ON DELETE CASCADE,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    artifact_id UUID NOT NULL REFERENCES input_artifacts(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('event', 'deadline', 'payment', 'form', 'amendment', 'instruction', 'info')),
    title TEXT NOT NULL,
    date DATE,
    end_date DATE,
    date_text TEXT,
    time_text TEXT,
    place TEXT,
    amount NUMERIC(14, 2),
    currency TEXT,
    amount_text TEXT,
    applies_to TEXT,
    subject_name TEXT,
    requires TEXT[] NOT NULL DEFAULT '{}',
    optional BOOLEAN NOT NULL DEFAULT FALSE,
    uncertain BOOLEAN NOT NULL DEFAULT FALSE,
    amends TEXT,
    change TEXT,
    quote TEXT NOT NULL,
    page INTEGER,
    span_start INTEGER,
    span_end INTEGER,
    boxes JSONB,
    match TEXT,                         -- exact | normalized | fuzzy; NULL when ungrounded
    confidence REAL NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX claims_extraction_idx ON claims (extraction_id, ordinal);
CREATE INDEX claims_artifact_idx ON claims (artifact_id);

-- What the family might do about a notice. Proposed until a member accepts
-- or dismisses it. A payment is a reminder: nothing here ever pays.
CREATE TABLE obligations (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    artifact_id UUID NOT NULL REFERENCES input_artifacts(id) ON DELETE CASCADE,
    claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    -- The child it is for, when the artifact names exactly one subject.
    subject_member_id UUID REFERENCES members(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('task', 'calendar')),
    action TEXT NOT NULL CHECK (action IN ('sign', 'pay', 'attend', 'submit', 'prepare', 'note')),
    title TEXT NOT NULL,
    due_date DATE,
    end_date DATE,
    due_time TEXT,
    optional BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'accepted', 'dismissed')),
    decided_by UUID REFERENCES members(id) ON DELETE SET NULL,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX obligations_household_idx ON obligations (household_id, status, due_date);
CREATE INDEX obligations_artifact_idx ON obligations (artifact_id);
