-- What FamilyOS did not see.
--
-- Everything so far records what arrived. The alpha's real question is the
-- other one: of the notices a family actually got, how many reached the
-- system at all? That cannot be answered from the artifacts, because a notice
-- that never arrived leaves no row.
--
-- So a member says so. "You missed the swimming letter -- it was on the school
-- portal." One row, and three decisions follow from the column that says
-- where it lived:
--
--   school_portal  -> the browser rail is worth building
--   school_app     -> the mobile rail is, if Play Integrity allows it
--   whatsapp_group -> forwarding needs to be easier, not a new rail
--   paper / word_of_mouth -> a camera in a native app
--   email          -> the query or the matching is wrong, and that is cheap
--
-- A gap is a report, never a notice. It produces no claims and no
-- obligations, and nothing downstream reads it: the moment it could, it would
-- be a second and unprovenanced source of truth about what a school said.
-- It exists to be counted.
--
-- Reported misses are a lower bound -- a family does not report every one --
-- so the useful number is the breakdown, not the rate.

CREATE TABLE intake_gaps (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    reported_by UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    -- What the notice was, in the family's own words. Deliberately not a
    -- claim: nobody extracts anything from this.
    title TEXT NOT NULL,
    -- Where it actually lived. The whole point of the table.
    lived_where TEXT NOT NULL CHECK (lived_where IN (
        'email', 'whatsapp_group', 'whatsapp_direct', 'school_portal', 'school_app',
        'other_app', 'sms', 'paper', 'word_of_mouth', 'unknown')),
    -- Was it also emailed? If it was, the gap is a matching problem rather
    -- than a channel problem, and those are much cheaper to fix.
    also_emailed BOOLEAN,
    -- Did it ask the family for something by a date? A missed newsletter and a
    -- missed payment deadline are not the same failure.
    had_date BOOLEAN NOT NULL DEFAULT FALSE,
    -- When the family found out, not when it was reported.
    noticed_on DATE,
    -- Set if the same notice later arrived by some route, so "we fixed that
    -- one" is answerable.
    arrived_as UUID REFERENCES input_artifacts(id) ON DELETE SET NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX intake_gaps_household_idx ON intake_gaps (household_id, created_at DESC);
CREATE INDEX intake_gaps_where_idx ON intake_gaps (lived_where);
