# WhatsApp SQL migrations (expand-only)

Run in **numeric order** against the shared PostgreSQL database. See **`../DEPLOYMENT_READINESS.md`** for the full deployment checklist.

| File | Idempotent patterns |
|------|---------------------|
| `001_whatsapp_credentials_expand.sql` | `IF NOT EXISTS`, `ON CONFLICT DO UPDATE` backfill |
| `002_whatsapp_bot_settings_expand.sql` | `ADD COLUMN IF NOT EXISTS`, `IF NOT EXISTS`, `ON CONFLICT` |
| `003_whatsapp_account_status_expand.sql` | Same as 002 |
| `004_whatsapp_account_capabilities_expand.sql` | `IF NOT EXISTS` |
| `005_whatsapp_usage_events_expand.sql` | `IF NOT EXISTS` |
| `006_whatsapp_usage_consumer_state_expand.sql` | `IF NOT EXISTS` |
| `007_whatsapp_platform_v2_expand.sql` | Onboarding sessions/events, trust & phone reputation snapshots, Meta app health, account restriction / safe mode / webhook / versioning columns |
| `008_whatsapp_webhook_health_expand.sql` | `webhook_last_validated_at`, `webhook_last_event_at`, `webhook_failure_reason`, `webhook_failure_count` |
| `009_onboarding_session_expand.sql` | `onboarding_sessions.expires_at`, `is_coexistence`, `onboarding_method` (+ optional backfill) |
| `010_whatsapp_accounts_safe_mode_operational_expand.sql` | `safe_mode_reason_code`, `safe_mode_override_expires_at`, `webhook_cooldown_ends_at`, `operational_metrics` |
| `011_whatsapp_accounts_last_error_code_type_normalize.sql` | Legacy `last_error_code` VARCHAR/TEXT → INTEGER on `whatsapp_accounts` |
| `012_whatsapp_operational_logs.sql` | `whatsapp_operational_logs` for verification timeline + safe-mode explainability |

Re-running a file after a partial failure is supported; fix the underlying error (permissions, lock, etc.) then re-apply.
