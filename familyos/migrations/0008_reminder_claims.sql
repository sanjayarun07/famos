-- Claiming a reminder before sending it.
--
-- `send_due` used to select pending rows, send them, then mark them sent.
-- Between the send and the update there was a window: two workers could both
-- pick up the same row, and a crash after delivery would deliver again on the
-- next sweep. One row per (obligation, member, reason, lead) stops a duplicate
-- being *scheduled*, which is not the same as stopping it being *sent*.
--
-- So a row is now claimed first -- moved to 'sending' in the same statement
-- that selects it, with SKIP LOCKED so a second worker passes over it -- and
-- only then handed to the channel. `claimed_at` is what lets a claim be taken
-- back: a worker that died mid-send leaves the row in 'sending' forever
-- otherwise.
--
-- This is at-least-once, not exactly-once, and it cannot be anything else
-- while the send is a network call to somebody else. What it does guarantee is
-- that a duplicate needs a crash in a window of milliseconds rather than a
-- second worker or an ordinary restart.

ALTER TABLE reminders DROP CONSTRAINT reminders_status_check;
ALTER TABLE reminders ADD CONSTRAINT reminders_status_check
    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled'));

ALTER TABLE reminders ADD COLUMN claimed_at TIMESTAMPTZ;

-- The delivery sweep looks for pending rows that are due, and for claims old
-- enough to have been abandoned.
DROP INDEX IF EXISTS reminders_due_idx;
CREATE INDEX reminders_due_idx ON reminders (status, send_after);
CREATE INDEX reminders_claimed_idx ON reminders (claimed_at) WHERE status = 'sending';
