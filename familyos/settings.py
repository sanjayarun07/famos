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

    # Extraction (familyos/extraction). "auto" uses the model when its
    # provider's API key is set and the offline rule-based extractor otherwise.
    # The model is named provider/model, LiteLLM-style.
    extraction_enabled: bool = True
    extractor: str = "auto"                 # auto | model | rules (claude, openai pick a provider)
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
