-- Tokens that end.
--
-- member_tokens has had revoked_at since milestone 1 and nothing has ever
-- written to it: there was no sign-out, no revoke, and no expiry, so a token
-- handed out once was good forever. The console made that worse by keeping one
-- in localStorage and calling a button "Sign out" that only forgot it locally.
--
-- A token now expires, slides forward while it is being used, and can be
-- revoked -- by the person holding it, or by a guardian on someone else's
-- behalf when a phone is lost.

ALTER TABLE member_tokens ADD COLUMN expires_at TIMESTAMPTZ;
ALTER TABLE member_tokens ADD COLUMN last_used_at TIMESTAMPTZ;
-- Why it ended, for the audit trail: signed_out | revoked | replaced.
ALTER TABLE member_tokens ADD COLUMN revoked_reason TEXT;

-- Tokens that already exist were issued with no end. Give them one rather than
-- leaving a class of immortal tokens behind, and date it from now so nobody is
-- signed out by the migration itself.
UPDATE member_tokens SET expires_at = NOW() + INTERVAL '30 days' WHERE expires_at IS NULL AND revoked_at IS NULL;

CREATE INDEX member_tokens_member_idx ON member_tokens (member_id) WHERE revoked_at IS NULL;
