# WhatsAppAccount trace (why it is a “God Node” and where to slice)

This document maps how `WhatsAppAccount` fans out across the microservice so you can decouple without guessing.

## 1) What `WhatsAppAccount` currently carries

Defined in `whatsapp/models.py`:

- **Identity / tenancy:** `workspace_id`, `connected_by_user_id`
- **Meta routing IDs:** `waba_id`, `phone_number_id`, display fields
- **Credentials:** `access_token_encrypted`, `token_type`, `token_expires_at` (vault slice: `whatsapp_credentials`)
- **AI (legacy mirror):** optional `ai_enabled`, `ai_model`, `temperature`, `max_tokens`, `prompt_override`, `knowledge_base_id` — canonical high-churn copy in `whatsapp_bot_settings`
- **Operational state:** `is_active`, `last_synced_at`, coexistence fields
- **Quality / heartbeat (legacy mirror):** `quality_score` plus optional `connection_status`, `is_verified`, `last_webhook_received`, `last_outbound_sent`, `last_error_*` — high-churn canonical copy in `whatsapp_account_status`
- **Flow crypto:** `flow_private_key`, `flow_public_key`

Risk called out in review: changing AI or UI-facing fields in the same row as encrypted tokens increases blast radius if a bug corrupts the row.

## 2) Read/write paths (high signal)

### Token read path (centralized)

- `WhatsAppAccount.get_access_token()` — used by most senders after an account row is loaded.
- `whatsapp/token_helper.py` — `get_account_with_token`, `get_valid_account_for_workspace` (also filters “has token”).

After Phase 1 credential slice:

- `get_access_token()` prefers `whatsapp_credentials.access_token_encrypted`, then falls back to `whatsapp_accounts.access_token_encrypted` (expand/contract safe).

### Token write path (OAuth / manual connect)

All of these call `set_access_token()` (shadow-writes both tables once migration + model are live):

- `whatsapp/connection_path.py` — manual connection / reconnect
- `whatsapp/account_service.py`
- `whatsapp/oauth.py`
- `whatsapp/routes.py` — embedded signup / token exchange variants
- `whatsapp/coexistence_routes.py`

### Token clear path

- `whatsapp/routes.py` — clears `access_token_encrypted` on disconnect / security flows  
  Must also clear `whatsapp_credentials` row (handled via `clear_access_token_storage()` on the model).

## 3) Downstream “blast radius” (what breaks if the account row is wrong)

| Area | Typical entry | Depends on account for |
| --- | --- | --- |
| Messaging | `whatsapp/messaging_service.py`, `whatsapp/services.py` | `phone_number_id`, decrypted token |
| Webhook | `whatsapp/webhook.py`, `whatsapp/routes.py` | routing to account, signature verification env |
| Templates | `whatsapp/template_service.py`, `whatsapp/template_routes.py` | `waba_id`, token |
| Flows | `whatsapp/flow_routes.py` | token, flow keys on account row |
| AI / automation | `whatsapp/interactive_automation_engine.py`, `whatsapp/automation_engine.py`, `whatsapp/ai_routes.py` | account id + token for outbound |
| Drip / bulk | `whatsapp/drip_engine.py`, `whatsapp/bulk_routes.py` | `account_id` FK on campaigns |
| Health | `whatsapp/health_check.py` | token for Meta probes |

If **token** data is corrupted, **send + template + flow + webhook-driven updates** fail first. If **identity** fields (`workspace_id`) are corrupted, authorization and multi-tenant isolation fail.

## 4) Slicing plan (matches “Credential / Bot / Status” strategy)

Phase 1 (this change set — **no column drops**):

1. **`whatsapp_credentials`** table — stores encrypted token + `phone_number_id` + `waba_id` (shadow-copied from account).
2. **Shadow write** on every `set_access_token` — updates credentials row **and** legacy columns.
3. **Preferential read** — `get_access_token` reads credentials first.

Phase 2 (bot settings — **implemented**, no column drops on `whatsapp_accounts` for AI fields):

1. **`whatsapp_bot_settings`** table — AI model, temperature, max tokens, prompt override, KB id, `ai_enabled` gate.
2. **Legacy columns** on `whatsapp_accounts` (`ai_enabled`, `ai_model`, …) — expanded in `002_whatsapp_bot_settings_expand.sql` for monolith parity; shadow-written from `whatsapp/ai_routes.py`.
3. **Runtime merge** — `get_bot_config()` / `prepare_automation_ai()` in `whatsapp/ai_chatbot.py` + `send_automation_response()` AI path prefer slice + legacy, then automation `response_config`.

Phase 3 (account status / heartbeat — **implemented**):

1. **`whatsapp_account_status`** — `status`, `quality_rating` (mirrors legacy `quality_score` for reads), `is_verified`, webhook/outbound timestamps, last Meta error fields.
2. **Legacy columns** on `whatsapp_accounts` from `003_whatsapp_account_status_expand.sql` for monolith CRM parity.
3. **Writes** — `update_account_heartbeat()` / `shadow_sync_account_quality_rating()` in `whatsapp/models.py`; called from `whatsapp/webhook.py` (messages, echoes, delivery status, errors, `account_update`), `whatsapp/services.py` (`_store_outgoing_message` outbound), `whatsapp/connection_path.py`, `whatsapp/account_service.py`.

**Capabilities (entitlement projection):** monolith-owned row per account — see `WHATSAPP_CAPABILITIES_CONTRACT.md` and `004_whatsapp_account_capabilities_expand.sql`.

## 5) Scheduler note (`SKIP LOCKED`)

Bulk “schedule send” uses `whatsapp_drip_campaigns.status = 'scheduled'` with `trigger_type = 'manual'` and ISO time in `trigger_value`.

`check_scheduled_campaigns()` in `whatsapp/drip_engine.py` already uses `FOR UPDATE SKIP LOCKED` when claiming rows. The tick endpoint remains `whatsapp/scheduler_routes.py` (`POST /api/internal/scheduler/tick`).

Optional: set `DRIP_SCHEDULE_USE_QUEUE=1` to enqueue per-campaign jobs instead of calling `trigger_campaign_now` inline (worker must be consuming the Redis queue).
