-- WhatsApp as an intake channel.
--
-- Most school communication in India happens in WhatsApp class groups and the
-- school's own app, not email. Meta's Cloud API cannot read a parent's groups
-- and never will -- but it can receive what a parent forwards to a business
-- number, which is the one manual habit parents already have.
--
-- Routing differs from email. An email carries the household's own address, so
-- the recipient says where it belongs. Every WhatsApp message arrives at one
-- business number, so the SENDER has to say: a member's phone number is what
-- places a message in a household.
--
-- That is better provenance than email, not worse. The whole authserv-id and
-- DKIM apparatus in intake/email.py exists because a From header is forgeable.
-- Meta verifies the number before it reaches us.

ALTER TABLE members ADD COLUMN phone TEXT;
-- Stored E.164 without the plus, as Meta reports it: 919876543210.
CREATE UNIQUE INDEX members_household_phone_idx ON members (household_id, phone) WHERE phone IS NOT NULL;
-- Finding the household from an incoming number is the hot path.
CREATE INDEX members_phone_idx ON members (phone) WHERE phone IS NOT NULL;

-- A child has no phone, for the same reason they have no email: they are a
-- data subject here, not an account.
ALTER TABLE members ADD CONSTRAINT members_child_has_no_phone CHECK (role <> 'child' OR phone IS NULL);

ALTER TABLE input_artifacts DROP CONSTRAINT input_artifacts_channel_check;
ALTER TABLE input_artifacts ADD CONSTRAINT input_artifacts_channel_check
    CHECK (channel IN ('upload', 'email', 'email_attachment', 'whatsapp', 'whatsapp_media'));
