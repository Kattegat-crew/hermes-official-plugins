# 03 — Verificación ejecutada (F5)

Fecha: 2026-11-03. Host: este (DEV), intérprete `/opt/hermes-agent/.venv/bin/python` (3.14.7).
Evidencia cruda: `/opt/hermes-agent-data/profiles/brokkr/evidence/t_6f336446/`.
La evidencia del fallo original (`.../evidence/t_aed35658/`) **no se tocó**: sigue registrando que el turno previo exportó 0 trazas.

## 1. TDD

| Fase | Comando | Resultado |
|---|---|---|
| RED | `pytest tests/test_sdk_provisioning.py -q` (antes de implementar) | 8 failed, 1 passed — `sdk_env` y `transport_health` no existían |
| GREEN (nuevo) | idem, tras implementar y provisionar el store | 9 passed, 0 skipped |
| GREEN (suite completa) | `PYTHONPATH=/opt/hermes-agent uv run --no-project --with pytest --python /opt/hermes-agent/.venv/bin/python pytest tests -q` | **234 passed** (baseline previo: 224 passed) |
| Copia canario | idem en `/opt/hermes-agent-data/fleet-canary/plugins/langfuse-fleet` | 180 passed, 2 skipped (los 2 skips ya existían al correr ese set sin el plugin importado) |

Nuevos tests (`tests/test_sdk_provisioning.py`): resolución del store (orden, override `pathsep`, exclusión del store 2.x), *append* después de `site-packages`, idempotencia, WARNING único accionable + `transport_health()` degradado, estado `ok` tras build correcto, contrato de manifiesto (`python_runtime: external` y `pm.read_python_declaration().is_member is False`), **proceso nuevo sin overlay** (langfuse 4.x desde el store; `pydantic`/`httpx` desde el venv *cuando el intérprete los provee*) y, desde la ronda 2, el caso `otlp` en proceso unitario y en proceso nuevo.

## 2. Provisión del store

```
bash langfuse-fleet/scripts/provision_sdk_store.sh
→ langfuse==4.15.2 → /opt/data/lazy-packages-langfuse4/py314
→ {"langfuse": "4.15.2", "langfuse_file": "/opt/data/lazy-packages-langfuse4/py314/langfuse/__init__.py",
   "pydantic_file": "/opt/hermes-agent/.venv/lib/python3.14/site-packages/pydantic/__init__.py",
   "httpx_file":   "/opt/hermes-agent/.venv/lib/python3.14/site-packages/httpx/__init__.py"}
```

El venv de Hermes y `/opt/data/lazy-packages` (langfuse 2.x) no se modificaron; `pydantic`/`httpx` siguen resolviéndose desde el venv, sin downgrades.

## 3. Turno real (proceso nuevo, sin overlay)

`run_real_turn_f5.py` → `HERMES_HOME=/opt/hermes-agent-data/fleet-canary`, `PYTHONPATH=/opt/hermes-agent` (sólo core), `sdk_overlay: null`, `HERMES_LANGFUSE_*` depurado del entorno.
Ruta de suscripción: `--provider openai-codex -m gpt-6.1-sol` → `billing_mode: subscription_included`, `estimated_cost_usd: 0.0` (sin fallback de pago).

| Señal | Resultado |
|---|---|
| exit code / duración | 0 / 17.8 s |
| stderr | vacío (ningún WARNING del plugin) |
| sesión | `20261103_165359_490e7e`, 2 llamadas API, 1 tool call (`read_file`), 4151 in / 49 out tokens |
| spool | 8 filas nuevas, **todas `done`**; total 33 filas, 0 pendientes; `delivered=33`, `batches_acked=16` |
| Langfuse | 1 traza `7829563b02d946d9ac0e44ea540dae5a` con `sessionId=20261103_165359_490e7e`, latencia 9.13 s, coste 0 |
| jerarquía | `Hermes turn` (root, parent null) → 2 × `Hermes generation` (GENERATION) + 1 × `Hermes tool` (SPAN) |
| metadata | allowlist exacta: `api_mode, api_request_id, model, platform, profile, provider, source, turn_id` |
| fuga del prompt | `marker_leak: false` (la marca del prompt no aparece en la traza) |

## 4. Estado degradado verificable (proceso nuevo)

`probe_degraded_path.py` con `HERMES_LANGFUSE_STORE_BASE` apuntando a un directorio inexistente, credenciales reales del canario:

- 1 solo WARNING, con `ModuleNotFoundError: No module named 'langfuse'`, el comando `scripts/provision_sdk_store.sh`, `HERMES_LANGFUSE_SDK_PATH`, stores buscados e intérprete.
- Segunda llamada a `_get_langfuse()`: 0 warnings extra.
- `transport_health()` → `{state: "degraded", reason: "ModuleNotFoundError: No module named 'langfuse'", python, stores}`.

También se verificó en proceso nuevo la resolución normal (`probe_canary_import.py`): store aplicado (`/opt/data/lazy-packages-langfuse4/py314`), `langfuse` importable, y estado `disabled` con causa cuando faltan credenciales (el `.env` lo carga el CLI, no un proceso suelto).

## 5. Admisión

`hermes plugins validate ./langfuse-fleet` → **Validation passed** (incluye el nuevo `python_runtime: external`). Los avisos de imports privados del core (`agent.redact`, `agent.usage_pricing`) son preexistentes y ajenos a F5.

## 6. No ejecutado (por gate)

`git add/commit/push/merge`, PR, despliegue a perfiles operativos, reinicio de gateways, activación de exporters de agentes. `git status` en el repo: `.specs/` y `langfuse-fleet/` sin trackear, igual que antes, sin commits nuevos.

## 7. Ronda 2 — cierre de los 2 defectos de la revisión (Heimdall, 2026-11-03 17:07)

Evidencia: `.../evidence/t_6f336446/r2/`.

### 7.1 Defecto 1 — AC3 no cubría el transporte por defecto (`otlp`)

Cambio: la excepción real del import se captura en cabecera (`_SDK_IMPORT_ERROR`) y `_report_missing_sdk(exc, transport)` es ahora común a los dos transportes (causa exacta, stores buscados, intérprete, `scripts/provision_sdk_store.sh`, `HERMES_LANGFUSE_SDK_PATH`); se eliminó el remedio imposible `hermes plugins install langfuse --force` y el `reason` genérico `langfuse SDK unavailable for the otlp transport`.

| Fase | Comando | Resultado |
|---|---|---|
| RED | `pytest langfuse-fleet/tests/test_sdk_provisioning.py -q` (antes del cambio) | **3 failed, 10 passed** — `red_otlp.txt`; el mensaje real era el genérico de Heimdall |
| GREEN (archivo) | idem | **13 passed** |
| GREEN (suite) | `PYTHONPATH=/opt/hermes-agent uv run --no-project --with pytest --python /opt/hermes-agent/.venv/bin/python -m pytest langfuse-fleet/tests -q` | **235 passed, 2 skipped** (`suite_canonical.txt`) |

Pruebas del caso `otlp`: (a) unitaria, parametrizada con `ModuleNotFoundError` y con `ImportError: cannot import name 'propagate_attributes'` (SDK 2.x resuelto desde otro store) — exige un único WARNING con causa, stores, override e intérprete, ausencia del remedio inválido y `reason` no genérico; (b) proceso nuevo real (`TestOtlpFreshProcessDegradedPath`).

Sondas de runtime con el plugin del canario y credenciales reales, transporte por defecto:

| Sonda | Escenario | Resultado |
|---|---|---|
| `probe_degraded_otlp.py` → `degraded_otlp_probe.json` | store forzado ausente, `HERMES_LANGFUSE_TRANSPORT` sin definir (= `otlp`) | transporte `otlp`; **1** WARNING: `Langfuse fleet (otlp transport): the Langfuse SDK is not importable (ModuleNotFoundError: No module named 'langfuse'); … provision the pinned store with bash …/scripts/provision_sdk_store.sh or point HERMES_LANGFUSE_SDK_PATH at it. Searched stores: …/absent-store/py314. Interpreter: /opt/hermes-agent/.venv/bin/python`; 2ª llamada = 0 warnings; `transport_health()` = `degraded`, `reason` con la excepción real |
| `probe_ok_otlp.py` → `ok_otlp_probe.json` | store provisionado, transporte por defecto | cliente construido (`Langfuse`), `langfuse 4.15.2` desde `/opt/data/lazy-packages-langfuse4/py314`, **0 warnings**, `transport_health()` = `ok` |
| `probe_degraded_path.py` → `degraded_legacy_probe_r2.json` | legacy (regresión) | 1 WARNING `(legacy transport)` con la misma información; `degraded` |

### 7.2 Defecto 2 — el test de "proceso nuevo" no era hermético

`test_plugin_resolution_exposes_sdk4_to_a_fresh_process` ya no asume que el intérprete provea `pydantic`/`httpx`: la sonda mide su resolución **antes** y **después** del bootstrap y exige (a) que el store quede después de `site-packages`, (b) `langfuse` 4.x y (c) que ningún paquete ya resuelto por el intérprete cambie de archivo. Donde el intérprete no los trae, el store los aporta (es un árbol `--target` completo), y eso no se presenta como precedencia del venv.

Verificación en un **venv 3.14 limpio** (sólo `pytest` + `ruamel.yaml`) con `PYTHONPATH=/opt/hermes-agent`, que es donde Heimdall reprodujo el fallo:

| Fase | Resultado |
|---|---|
| ronda 1 (informe del revisor) | 1 failed, 233 passed — `pydantic_file` venía del store |
| ronda 2 | **235 passed, 2 skipped, 0 failed** (`hermetic_suite_cleanvenv.txt`) |

Docs corregidos: `README.md` (store completo ~31 MB, orden vs precedencia, estado degradado en ambos transportes), `02-design.md` §1 y §3, `01-spec.md` AC1/AC3.

### 7.3 Turno real de la ronda 2 (proceso nuevo, sin overlay)

`run_real_turn_r2.py`: `HERMES_HOME=/opt/hermes-agent-data/fleet-canary`, `PYTHONPATH=/opt/hermes-agent` (sólo core), `sdk_overlay: null`, `HERMES_LANGFUSE_*` depurado del entorno, ruta de suscripción `--provider openai-codex -m gpt-6.1-sol`.

| Señal | Resultado |
|---|---|
| exit / duración / stderr | 0 / 18.4 s / **vacío** |
| sesión | `20261103_171516_d289b2`, 2 llamadas API, 1 tool call, `billing_mode=subscription_included`, coste 0.0 |
| spool | **8 filas nuevas, todas `done`** (41 totales, 0 pendientes); `delivered=41`, `batches_acked=22` |
| Langfuse (HTTP crudo) | 1 traza `09de998d44674aa5ad890cccfc4bee3c`, `sessionId=20261103_171516_d289b2`, 4 observaciones: `Hermes turn` (root, parent null) → 2 × `Hermes generation` + 1 × `Hermes tool` |
| metadata | allowlist exacta de 8 claves; `input`/`output` nulos; `marker_leak: false` |

## 8. Caveat de compatibilidad (no bloqueante, leer antes del plan de 24 h)

El servidor es Langfuse **2.95.11** y el SDK pineado es **4.15.2**. El plugin sólo usa el cliente REST de bajo nivel (`langfuse.api.client.LangfuseAPI`), por lo que la entrega no se ve afectada; pero las APIs **tipadas de lectura** del SDK 4 (`api.trace.get(id)`, etc.) validan campos de la forma v3 (`environment`, `usage_details`, `cost_details`) que un servidor v2 no devuelve: fallan con `ValidationError`. Todo readback/verificación debe hacerse por **HTTP crudo** (`/api/public/traces`, `/api/public/observations`) como en `readback_r2.py`, no con `api.trace.get`.
