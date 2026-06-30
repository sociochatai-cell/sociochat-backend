# Backend WhatsApp Rebase — Reconciliation Map (Phase 2 blueprint)

SOURCE (newer, single-tenant, env creds): `bug_fix-Auto-Inter/.../whatsapp-service/whatsapp` (109 .py)
TARGET (older, multi-tenant + CRM): `sociochat-backend/whatsapp` (67 .py)

## GOLDEN RULE
Anywhere SOURCE reads a credential / AI key / api-version / app-secret from `os.getenv` or a
module constant, wrap it in TARGET's `get_tenant_meta_config(...) or env-fallback` pattern.
And the entire `SocioviaCrm.*` pipeline (webhook.py + interactive_automation_engine.py) must be
re-grafted — SOURCE has none of it. Do NOT delete the 9 target-only files.

Also note (cross-module, handled in Phase 1): SOURCE whatsapp imports from `core/`, `shared_models`,
`monolith_integration`, `integrations`, `subscription`, `rag*`, `crm_bootstrap`. These supporting
modules must be reconciled with the target (single `db` from `models`, target `subscription`,
in-process monolith_integration) before the port imports cleanly.

## Classification
- BOTH = 57 files (merge). SOURCE-ONLY = 53 (add as-is). TARGET-ONLY = 9 (KEEP).
- workspace_id typing already consistent across both (WhatsAppAccount=String(255), FavoriteSticker=Integer).
- Attribution fields (entry_source, ctwa_clid, ad_id, campaign_id, adset_id, attribution_data)
  ALREADY in source models.py + webhook.py — take source, just verify.

### TARGET-ONLY (must keep — source must not delete)
blog_models.py, blog_routes.py, booking_reminders.py, coexistence_service.py, dataset_models.py,
dataset_routes.py, flow_os_models.py, flow_os_routes.py, scheduler.py

### SOURCE-ONLY (new features to ADD)
account_service, admin_verification_routes, agentos_handoff, ai_job, background_processor,
bml_carousel_template, capabilities, capabilities_routes, crm_lead_models, debug_logger, fast_router,
flow_field_labels, flow_inbox_sync, http_rate_limit, human_escalation, lead_action_service,
lead_growth_models, lead_growth_routes, lead_intent_router, messaging_service, meta_asset_discovery,
onboarding_routes, onboarding_session_manager, operational_profile, order_enrichment,
partner_templates, provisioning_engine, provisioning_types, safe_mode_engine,
tech_provider_onboarding, template_service, test_input_node_handler, trace_debug,
trust_app_health_aggregate, trust_reputation_poll, trust_signal_collector, trust_snapshot_diff,
trust_snapshot_engine, trust_snapshot_routes, trust_snapshot_scheduler, vaish_carousel_template,
verification_routes, warmup_account_ops, warmup_config, warmup_enforcement, warmup_engine,
warmup_routes, warmup_scheduler, warmup_signals, warmup_types, webhook_health, webhook_routing,
webhook_utils
(several — messaging_service, lead_action_service, human_escalation, fast_router, agentos_handoff,
account_service, provisioning_engine — are env-credential; harden with tenant pattern when wired in.)

## Merge actions (A=take source, B=source + re-apply hooks, C=hand-merge)

### C — hand-merge (highest risk)
- routes.py: 27 tenant call sites. Re-apply per-tenant get_tenant_meta_config (~13 sites); replace
  source `verify_webhook_signature_any` (env) with target `verify_token_matches_any(token)` +
  `get_tenant_meta_config(phone_number_id=pid).app_secret`. OAuth/token-exchange routes
  (src ~6325/6650/7019 use META_APP_SECRET/os.getenv) → tenant-resolved. Source heavily expanded; merge route-by-route.
- webhook.py: source has NO SocioviaCrm. Take source skeleton, re-insert full CRM pipeline (see appendix).
- __init__.py: blueprint registry must UNION target-only (blog_routes, dataset_routes, flow_os_routes)
  + new source blueprints.

### B — source + re-apply hooks
- oauth.py: 8 get_tenant_meta_config sites (app_id/secret/redirect, env fallback).
- services.py / messaging_service.py: tenant api_version + `_get_workspace_account` lookup.
- drip_routes.py: tenant google_sa_json; reconcile enroll_from_crm signature (target (..,workspace_id) vs source (..,workspace_id,commit=True)).
- ai_chatbot.py: `_get_tenant_genai_client` (per-tenant Gemini key) + `_genai_clients_by_key` cache.
- interactive_automation_engine.py: re-add 3 SocioviaCrm.lead_ingest hooks (advance_lead_status/set_lead_status).
- catalog_routes.py: tenant.context ownership guard (get_current_user + user_owns_workspace).
- coexistence_routes.py: get_tenant_meta_config; verify import of target-only coexistence_service.
- template_rewriter.py: get_tenant_ai_config.
- ai_routes.py: verify consumes _get_tenant_genai_client after ai_chatbot merge.

### A — take source as-is (~38 files, diff-verify)
models.py (verify FK/relationships vs the 4 target-only model modules), automation_engine/models/routes,
drip_engine/drip_models, bulk_routes, trigger_routes/models/logs/production_trigger_routes,
flow_* (access/endpoint/routes/testing/validator/variables), template_* (builder/node_executor/routes/validator),
node_executor, api_node_executor, input_node_handler, intent_detection, conversation_state_engine,
faq_models/faq_routes, knowledge_routes, scheduler_routes, tracking_routes, usage_events(+routes),
connection_guard, connection_path, encryption, health_check, rate_limiter, token_helper, utils,
validators, visual_automation_models, interactive_automation_routes, interactive_flow_ai.

## APPENDIX — exact target hooks to preserve

### Per-tenant credential / scoping
- oauth.py L49-52, L76-78, L125-128, L342-343: get_tenant_meta_config(workspace_id) → redirect_url, app_id||META_APP_ID, app_secret||META_APP_SECRET.
- routes.py L536-537: verify_token_matches_any(token) for webhook GET verify.
- routes.py L571-572: app_secret = get_tenant_meta_config(phone_number_id=pid).app_secret (POST signature).
- routes.py L2992/3424/3744/4324/4513/4607/4673/4772: api_version = get_tenant_meta_config(workspace_id=...).whatsapp_api_version or "vXX".
- routes.py L4731/4884/5150/5601: cfg = get_tenant_meta_config(workspace_id=workspace_id).
- services.py L73-77 + L2578-2579: tenant whatsapp_api_version + cfg.
- coexistence_routes.py L228-231: cfg.app_id||FB_APP_ID, cfg.app_secret||FB_APP_SECRET.
- catalog_routes.py L53-74: get_current_user + user_owns_workspace → raise "forbidden" if not owned.
- ai_chatbot.py L159-174: _get_tenant_genai_client → cfg.gemini_api_key + cache.
- drip_routes.py L36-44: tenant google_sa_json. template_rewriter.py L68: get_tenant_ai_config.

### CRM hooks (SocioviaCrm)
- webhook.py L50: `from SocioviaCrm.capi_service import send_capi_event` (used L586, L941).
- webhook.py L400-404: advance_lead_status_from_conversation(conv, account, "qualified", reason="Completed WhatsApp flow", db_session=...).
- webhook.py L426-431: upsert_lead_from_conversation(conv, account, db_session=...) (new-conversation capture).
- webhook.py L437-450: refresh + advance_lead_status_from_conversation(..,"contacted","Replied on WhatsApp",..).
- webhook.py L476-485: match_qualify_rule(text, workspace_id) + advance_lead_status_from_conversation(.., rule["status"], ..).
- webhook.py L491-497: classify_lead_status(text, workspace_id) AI fallback (only when rule is None).
- webhook.py L998: advance_lead_status_from_conversation(...).
- interactive_automation_engine.py L462-469, L1135, L1386: SocioviaCrm.lead_ingest advance_lead_status/set_lead_status on node transitions.
- drip_routes.py L1960: def enroll_from_crm(entity_type, entity_data, workspace_id) — reconcile with source signature.

### Attribution (already in source — verify only)
models.py: entry_source/ctwa_clid/ad_id/campaign_id/adset_id/attribution_data (source L1205-1210 == target L486-491).
webhook.py attribution logic present in source L1228-1300+.
