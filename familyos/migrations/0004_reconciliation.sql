-- Milestone 3, part two: reconciliation. A school sends "Annual day, revised
-- timings" and the old notice is still sitting there with its own date. Until
-- now both reminded, and nothing said they were the same thing.
--
-- An amendment claim already says, in its own words, what it changes
-- (claims.amends) and what is now true (claims.change). What was missing was
-- the link from that claim to the artifact it is talking about.
--
-- The link is PROPOSED, never applied on its own. Getting it wrong means the
-- family stops being reminded about a deadline that still stands, which is
-- worse than being reminded twice, so a person confirms it. Until they do,
-- both notices still remind -- but the reminder says a later notice may have
-- changed it.

CREATE TABLE amendments (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    -- The notice doing the revising, and the claim in it that says so.
    artifact_id UUID NOT NULL REFERENCES input_artifacts(id) ON DELETE CASCADE,
    claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    -- What it revises.
    amends_artifact_id UUID NOT NULL REFERENCES input_artifacts(id) ON DELETE CASCADE,
    amends_claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    matched_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    decided_by UUID REFERENCES members(id) ON DELETE SET NULL,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (claim_id, amends_claim_id),
    CHECK (artifact_id <> amends_artifact_id)
);
CREATE INDEX amendments_household_idx ON amendments (household_id, status, created_at DESC);
CREATE INDEX amendments_amends_idx ON amendments (amends_artifact_id);

-- An obligation the family no longer has to do, because a later notice
-- replaced the thing it came from. It stays on the record: superseded, not
-- deleted, so "what happened to that?" has an answer.
ALTER TABLE obligations ADD COLUMN superseded_at TIMESTAMPTZ;
ALTER TABLE obligations ADD COLUMN superseded_by_artifact_id UUID REFERENCES input_artifacts(id) ON DELETE SET NULL;
CREATE INDEX obligations_live_idx ON obligations (household_id, status) WHERE superseded_at IS NULL;
