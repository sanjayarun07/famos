-- Connected Google mailboxes.
--
-- A member grants read-only access to their own Gmail and FamilyOS polls it
-- for school mail. The refresh token is the whole of that access, so it is
-- sealed with the household's data key rather than stored in the clear: the
-- erasure path destroys that key first, which means erasing a household also
-- ends its access to every mailbox it was reading. Nothing has to remember to
-- go and revoke anything.
--
-- `history_id` is Gmail's own cursor. Keeping it means a poll asks for what
-- changed rather than re-reading the mailbox, and a restarted poller does not
-- replay a year of mail.

CREATE TABLE google_accounts (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    -- The Google account, for showing the member which mailbox this is. Not a
    -- way in: authorisation is the token, and the token is sealed.
    email TEXT NOT NULL,
    -- Google's stable id for the account, which survives an address change.
    subject TEXT NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT '{}',
    sealed_refresh_token BYTEA NOT NULL,
    history_id BIGINT,
    -- Where an interrupted first pass got to, so it can be finished without
    -- starting again.
    backfill_cursor TEXT,
    backfill_done BOOLEAN NOT NULL DEFAULT FALSE,
    last_polled_at TIMESTAMPTZ,
    -- Google will not refresh this any more: the member revoked access or
    -- changed their password. Only they can fix it, so the poller stops
    -- asking and the console asks them to reconnect.
    needs_reconnect BOOLEAN NOT NULL DEFAULT FALSE,
    last_error TEXT,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disconnected_at TIMESTAMPTZ
);

-- One live connection per Google account per member. Reconnecting the same
-- mailbox replaces the token rather than making a second row; a disconnected
-- row is kept so the audit trail still says it was once connected.
CREATE UNIQUE INDEX google_accounts_live_idx
    ON google_accounts (member_id, subject) WHERE disconnected_at IS NULL;
CREATE INDEX google_accounts_household_idx ON google_accounts (household_id, disconnected_at);

-- Short-lived state for an authorisation in flight. A callback that cannot be
-- matched to one of these rows is not ours and is refused, which is what stops
-- someone else's consent being attached to this member.
CREATE TABLE google_auth_states (
    state TEXT PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    code_verifier TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    used_at TIMESTAMPTZ
);
CREATE INDEX google_auth_states_created_idx ON google_auth_states (created_at);

-- Two more ways a notice can arrive: the mail itself, and anything attached
-- to it. Kept distinct from 'email' because the trust questions differ --
-- forwarded mail has a sender to authenticate, a polled mailbox does not.
ALTER TABLE input_artifacts DROP CONSTRAINT input_artifacts_channel_check;
ALTER TABLE input_artifacts ADD CONSTRAINT input_artifacts_channel_check
    CHECK (channel IN ('upload', 'email', 'email_attachment', 'whatsapp', 'whatsapp_media',
                       'gmail', 'gmail_attachment'));
