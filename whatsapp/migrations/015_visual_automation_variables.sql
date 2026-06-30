-- Per-flow runtime variables (API tokens, base URLs) and optional flow_config (defaults, capture rules).
-- Secrets live here — not in application env vars.

ALTER TABLE whatsapp_visual_automations
    ADD COLUMN IF NOT EXISTS variables JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE whatsapp_visual_automations
    ADD COLUMN IF NOT EXISTS flow_config JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN whatsapp_visual_automations.variables IS
    'Flat key/value map for {{placeholder}} substitution in API nodes (includes secrets).';

COMMENT ON COLUMN whatsapp_visual_automations.flow_config IS
    'Flow runtime config: variableDefaults, optional global buttonCaptureRules.';
