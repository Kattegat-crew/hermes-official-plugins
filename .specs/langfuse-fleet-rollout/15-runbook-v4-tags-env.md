# 15 — Runbook v4: tags `env:`/`bot:` y despliegue por copias locales (2026-11-09)

Estado: **ejecutado y verificado en DEV y PROD** (2026-11-09). Este runbook documenta el
incidente del bucle OTLP y el contrato nuevo de atribución; no re-decide la arquitectura
F5 (Opción B + `HERMES_LANGFUSE_TRANSPORT=legacy`).

## 1. Causa raíz del incidente

1. Langfuse self-hosted **2.95.11** es REST-only: `POST /api/public/otel/v1/traces` → **404**.
2. `langfuse-fleet` usa **OTLP por defecto** si falta `HERMES_LANGFUSE_TRANSPORT`; el plugin
   catálogo `langfuse` no tiene modo legacy.
3. Un proceso long-lived (dashboard) conservó en memoria el modo OTLP tras cambiar los `.env`:
   siguió exportando 404 y provocó `event loop stalled` de 20–24 s.
4. Las **copias por-perfil** del plugin (`profiles/<p>/plugins/langfuse-fleet`) son las que
   cargan los turnos de cron/kanban. Actualizar solo la copia compartida deja esas rutas con
   el código viejo (sin tags ni `metadata.env`).

## 2. Contrato canónico

Variables por home/perfil (valores no secretos; las claves viven en Vault):

```env
HERMES_LANGFUSE_PUBLIC_KEY=<vault>
HERMES_LANGFUSE_SECRET_KEY=<vault>
HERMES_LANGFUSE_BASE_URL=http://100.73.30.29:3010
HERMES_LANGFUSE_HOST=http://100.73.30.29:3010
HERMES_LANGFUSE_ENV=development        # production en PROD
HERMES_LANGFUSE_RELEASE=v0.21.3-ncl
HERMES_LANGFUSE_MAX_CHARS=30000
HERMES_LANGFUSE_TRANSPORT=legacy
HERMES_LANGFUSE_CAPTURE=metadata
HERMES_LANGFUSE_SAMPLE_RATE=1.0
```

Matriz de plugins en root + **todos** los perfiles:

```yaml
plugins:
  enabled:
    - langfuse-fleet
  disabled:
    - langfuse
    - observability/langfuse
```

## 3. Despliegue (idempotente)

1. Actualizar la copia compartida del plugin:
   `$HERMES_HOME/plugins/langfuse-fleet/{__init__.py,legacy_ingestion.py}`.
2. Actualizar **todas** las copias locales:
   `$HERMES_HOME/profiles/<perfil>/plugins/langfuse-fleet/{__init__.py,legacy_ingestion.py}`.
3. Asegurar la matriz de plugins en `$HERMES_HOME/config.yaml` y en cada
   `profiles/<perfil>/config.yaml` (fleet enabled; catálogo/observability disabled).
4. Asegurar las 10 variables en `$HERMES_HOME/.env` y en cada `profiles/<perfil>/.env`
   (sin secretos en git; hidratar desde Vault).
5. Reiniciar los procesos que cargan el plugin:
   - DEV: `hermes-dashboard`, `hermes-agent`, `hermes-agent-roshi` (y cualquier gateway
     standalone de perfil).
   - PROD: contenedor `hermes-agent` (y gateways standalone del contenedor).
6. Validar YAML con PyYAML antes de reiniciar. Un YAML inválido paraliza todas las tools
   del perfil.

## 4. Contrato de atribución (plugin)

`langfuse-fleet` escribe en cada `trace-create` legacy:

- `metadata.profile` = perfil que ejecuta el turno.
- `metadata.env` = `development` | `production`.
- `tags` = base (`hermes`, `langfuse`, `platform:<p>`, `trigger:<cron|interactive>`,
  `cron-job:<id>`) + `env:<env>` + `bot:<perfil>`.

Esto alimenta el filtro por tags de la UI/API de Langfuse y las cards 53/54 del dashboard
Metabase id 5 (`Control de Flota DEV vs PROD`), que parsean `tags[env:]` y `tags[bot:]`.

Cobertura: un trace por turno, una generation por llamada LLM (usage/cost), un span por
tool call (nombre `Tool: <tool_name>`, metadata `tool_name`, `tool_call_id`, `skill`) y un
span por subagente. `skill` se extrae de args de `skill_view`/`skill_manage` o de
`Skill recomendada: <name>` en el resultado de `skill_route`; no hay un span dedicado de skill.

## 5. Verificación

- `pytest langfuse-fleet/tests -q` → **235 passed, 3 skipped, 1 failure preexistente**
  (`test_sdk_provisioning::test_pm_reads_the_plugin_as_a_non_member`, atributo
  `PythonDeclaration.is_member` ausente en el core actual; reproducido en la rama base).
- Canary: `F3_RESULT ok=true`, `transport=legacy`, `capture=metadata`,
  `assertion_all_delivered=true`, `assertion_allowlist_clean=true`.
- `errors.log`: **0** `Failed to export span batch` posteriores al corte.
- Spool: todos los eventos `done`; sin `pending`/`in-flight`.
- Atribución tool/skill: `select metadata->>'tool_name', metadata->>'skill', count(*) from observations group by 1,2` → los spans de tool llevan `Tool: <name>` y `skill` cuando aplica.
- Traza real de cron verificada: `jacqueline` 2026-11-09T22:43:16Z con
  `env=production`, tags `bot:jacqueline,cron-job:…,env:production,…`.
- Metabase: DS id 2 `Langfuse Production` → `dbname=langfuse`; dashboard 5 con cards
  53/54 clasificando el tráfico nuevo. Las trazas históricas sin tag quedan
  `unassigned` (no se reescriben).

## 6. Rollback

1. Restaurar los backups del plugin y de los `.env`/`config.yaml` del run de despliegue.
2. Volver a la revisión anterior de la rama pin del plugin.
3. Reiniciar los mismos servicios.
4. Confirmar que el spool no tenía `pending>0` antes del rollback; si lo había, preservarlo
   con la API de backup de SQLite (nunca `cp` de un spool vivo).
