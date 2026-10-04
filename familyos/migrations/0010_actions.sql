-- Doing something about a notice.
--
-- Two kinds, and the enum is the point: there is no 'payment', no 'browse',
-- no 'purchase'. What an agent may do is a database constraint rather than a
-- policy document, so widening it is a migration somebody has to write and
-- review.
--
--   reply     -- answer the school that wrote. The commonest thing a notice
--                asks for is "let us know by Friday", and answering it needs
--                no browser, no credential beyond our own SMTP, and no
--                automation of anybody else's service.
--   calendar  -- a dated thing, as an .ics a mail client will offer to add.
--                No calendar write scope, so no new permission from anyone.
--
-- Three properties make this safe enough to ship in an alpha:
--
-- 1. The recipient is DERIVED from the notice, never supplied. A caller sends
--    an obligation id and a message; the address comes from the artifact's
--    stored source. A sender that accepts an arbitrary address is an
--    exfiltration primitive, and the column is written server-side only.
--
-- 2. The approval is bound to the exact parameters. params_sha256 is computed
--    over recipient, subject and body when proposed, and the approving request
--    must echo it back. If anything changed in between -- by a bug, by a
--    second proposal, by anything -- the approval does not apply to what is
--    now stored and is refused.
--
-- 3. One send per action, claimed before sending, the same way a reminder is.

CREATE TABLE actions (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    obligation_id UUID NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
    -- Kept so the receipt can point at the notice even if the obligation is
    -- superseded by a later reading.
    artifact_id UUID NOT NULL REFERENCES input_artifacts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('reply', 'calendar')),

    -- Derived, never supplied by a caller.
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    -- What the approval is an approval OF.
    params_sha256 TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'approved', 'sending', 'sent', 'failed', 'cancelled')),
    proposed_by UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    approved_by UUID REFERENCES members(id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One live action per kind per obligation: proposing a reply twice gives one
-- reply to decide about, not two to send.
CREATE UNIQUE INDEX actions_live_idx ON actions (obligation_id, kind)
    WHERE status NOT IN ('sent', 'failed', 'cancelled');
CREATE INDEX actions_household_idx ON actions (household_id, created_at DESC);
CREATE INDEX actions_sendable_idx ON actions (status, approved_at);
