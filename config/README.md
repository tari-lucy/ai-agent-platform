# Bootstrap configuration M0-06

Runtime validates before core shell / worker check. Defaults are versioned by
BootstrapSettings schema 1.0. The editable JSON example is only a workspace
draft, not an accepted persona canon (M2). No keys/endpoint in JSON.

Order: typed defaults → explicit workspace profile → local JSON overrides →
explicit local .env binding/process environment. Process env wins per field.
AI_PLATFORM_WORKSPACE_PROFILE / AI_PLATFORM_LOCAL_OVERRIDES /
AI_PLATFORM_ENV_FILE are optional explicit paths. No implicit .env discovery.
Standalone checks: ai-platform-core-check / ai-platform-worker-check.
For native checks, set AI_PLATFORM_ENV_FILE to your local binding if needed;
the agent never reads the real file for diagnostics.

.env.example is an editable reference with blank bindings. M0-04 initializer
creates .env if absent, then edit that local file without overwriting it.
Docker Compose reads its explicit .env and forwards only REDIS_PASSWORD/core
port as before; root config paths/overrides are not automatically mounted or
applied to containers. Core/worker entrypoints now validate before listener/heartbeat. A later deployment
owner must bind selected external paths/settings;
M0-06 Linux probe mounts only synthetic configuration and current sources.

Autostart must remain false. Every new bootstrap gets a fresh epoch and
STOPPED/effects_enabled=false, even if a caller supplies persisted ACTIVE.
This receipt is not the M1 STOP/revocation supervisor or an API contract.
No jobs/LLM/client/DB are created. Resource settings are validated intentions,
not dynamic changes to existing Compose CPU/memory limits. Port settings are
validated here; only deployment controls actual listener/published port.

Errors show known field + fixed reason (range/schema/type), without input,
paths, secret values, exception context or raw JSON. Extra JSON keys are
rejected. Endpoint is SecretStr and never included in the safe manifest.
Finite settings stay within the accepted M0 budgets; raising ceilings belongs
to a reviewed configuration/resource change. Full groups (access, retention,
role capabilities, development, integrations) are implemented by their owners.
