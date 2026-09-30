-- Milestone 3, part one: reminders. An obligation with a date is a thing the
-- family has to remember; until now nothing ever told them.
--
-- One row per (obligation, person, reason, lead time), so the sweep that
-- creates them can run as often as it likes and never send twice. Who gets a
-- row is decided by the same visibility rule as everything else: a private
-- notice reminds only the member who sent it.

CREATE TABLE reminders (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    obligation_id UUID NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
    -- Who is being reminded. Never a child: children do not sign in and are
    -- not told things; the obligation's subject is who it is *about*.
    member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    -- due:       an accepted obligation is coming up.
    -- undecided: a proposal nobody has accepted or dismissed, and its date is
    --            close. The one most likely to be missed.
    reason TEXT NOT NULL CHECK (reason IN ('due', 'undecided')),
    lead_days INTEGER NOT NULL,
    send_after TIMESTAMPTZ NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (obligation_id, member_id, reason, lead_days)
);
CREATE INDEX reminders_due_idx ON reminders (status, send_after);
CREATE INDEX reminders_member_idx ON reminders (member_id, send_after DESC);
CREATE INDEX reminders_household_idx ON reminders (household_id, created_at DESC);
