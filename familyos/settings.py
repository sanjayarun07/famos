"""Configuration, read from the environment (prefix FAMILYOS_) or a .env file."""
from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FAMILYOS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/familyos"

    # Wraps every household's data key. 32 bytes, base64. Rotating it means
    # re-wrapping the household keys, never re-encrypting the blobs.
    master_key: str = Field(default="", repr=False)

    # Object storage. "local" writes under blob_dir (dev and tests); "s3"
    # talks to any S3-compatible store (AWS, MinIO, R2).
    blob_backend: str = "local"
    blob_dir: str = ".blobs"
    s3_bucket: str = "familyos-originals"
    s3_endpoint_url: str | None = None
    s3_region: str = "ap-south-1"
    s3_access_key_id: str | None = Field(default=None, repr=False)
    s3_secret_access_key: str | None = Field(default=None, repr=False)

    # Intake limits.
    max_upload_bytes: int = 25 * 1024 * 1024
    max_email_bytes: int = 30 * 1024 * 1024

    # Forwarding email: <household inbound token>@<inbound_domain>. The mail
    # provider posts the raw message to /v1/inbound/email with this secret.
    # How long a sign-in token lasts. It slides forward while it is being
    # used, so an active session stays signed in and an abandoned one ends.
    token_lifetime_days: int = 30

    inbound_domain: str = "in.familyos.local"
    inbound_webhook_secret: str = Field(default="", repr=False)
    # WhatsApp Cloud API. Parents forward school messages to this business
    # number; Meta has already verified the sender, so a number that belongs to
    # a member is trusted and one that belongs to nobody is refused.
    # app_secret signs every delivery: without it nothing is accepted.
    whatsapp_app_secret: str = Field(default="", repr=False)
    whatsapp_verify_token: str = Field(default="", repr=False)
    whatsapp_access_token: str = Field(default="", repr=False)
    whatsapp_phone_number_id: str = ""
    # The number as a parent would save it. Meta's phone_number_id is an
    # internal handle and no use to anyone forwarding a message.
    whatsapp_business_number: str = ""
    # A development bridge for unofficial WhatsApp clients, which can read a
    # class group where the Cloud API cannot. Off by default and meant to stay
    # that way: those clients break WhatsApp's terms, the ban lands on the
    # linked number, and a companion session can read every chat on it.
    whatsapp_bridge_enabled: bool = False
    whatsapp_bridge_secret: str = Field(default="", repr=False)
    whatsapp_api_base: str = "https://graph.facebook.com/v21.0"

    # Quarantine mail whose From address is not authenticated (DMARC, or
    # DKIM/SPF aligned) by the receiving provider.
    require_sender_auth: bool = True
    # The authserv-id our mail provider stamps on its Authentication-Results
    # header (RFC 8601): "mx.google.com", "amazonses.com", the MX hostname.
    # Only that provider's verdict is believed. Unset, nothing authenticates
    # and every message waits in quarantine for a guardian.
    inbound_authserv_id: str = ""

    # Durable jobs (familyos/jobs.py, from Orbit).
    jobs_enabled: bool = True
    job_lease_seconds: float = 60.0
    job_max_attempts: int = 5
    job_attach_seconds: float = 20.0

    # Extraction (familyos/extraction). "rules" reads notices in this
    # deployment and sends nothing anywhere. "model" sends the full text of
    # every accepted notice to the provider of extraction_model, so it is named
    # here and never inferred from an API key being present.
    # The model is named provider/model, LiteLLM-style.
    extraction_enabled: bool = True
    extractor: str = "rules"                # rules | model | claude | openai
    extraction_model: str = Field(default="openai/gpt-5.6-sol",
                                  validation_alias=AliasChoices("FAMILYOS_EXTRACTION_MODEL", "RESEARCH_MODEL"))
    extraction_effort: str = "medium"
    anthropic_api_key: str = Field(default="", repr=False,
                                   validation_alias=AliasChoices("FAMILYOS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"))
    openai_api_key: str = Field(default="", repr=False,
                                validation_alias=AliasChoices("FAMILYOS_OPENAI_API_KEY", "OPENAI_API_KEY"))

    # Reminders (familyos/reminders.py). One recurring job tells whoever may
    # see an obligation, before its date. Lead times are days before, as a
    # comma-separated list; 0 means on the day.
    reminders_enabled: bool = True
    reminder_lead_days: str = "7,1,0"
    reminder_sweep_seconds: float = 3600.0
    reminder_max_attempts: int = 5
    # "log" writes the reminder to the application log and the audit trail and
    # is the default because it needs nothing configured. "email" needs SMTP.
    reminder_channel: str = "log"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_username: str = ""
    smtp_password: str = Field(default="", repr=False)
    smtp_from: str = ""

    # Reconciliation (familyos/reconcile.py). Links a revised notice to the one
    # it revises. Always proposed, never applied on its own.
    reconciliation_enabled: bool = True


settings = Settings()
