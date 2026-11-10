# 03 — Desglose de Tareas (F3+, bloqueado hasta aprobación del reviewer)

Orden TDD: cada tarea crea primero su prueba (RED), luego la implementación (GREEN). Verificación = comando automatizado con exit 0. Sin dependencias no declaradas. Tareas marcadas [F1] ya ejecutadas en esta fase.

## F1 — Recuperación y diseño [COMPLETADAS]

- [x] T-01 Validar tar (traversal/symlinks/hardlinks) y extraer a `/opt/hermes-official-plugins` con permisos mínimos. *Verif:* sha256 del tar = `c380bc38…f0`; `git fsck --full` limpio; `git status --porcelain` = 0 entradas.
- [x] T-02 Confirmar parentado `c2a95963 → e52545c → de4c12af` y worktree == HEAD. *Verif:* `git log --format='%H %P'` y `git diff HEAD` vacío.
- [x] T-03 Re-ejecutar suite completa. *Verif:* `PYTHONPATH=/opt/hermes-agent /usr/bin/python3.12 -m pytest -q` → **155 passed, 2 skipped**.
- [x] T-04 Re-ejecutar contrato SDK standalone. *Verif:* `python3 tests/test_legacy_sdk_contract.py` (venv PM) → exit 0.
- [x] T-05 Documentar pre-flight: AGENTS.md/plugins/AGENTS.md del host aplicados; ausencias de reglas propias del repo registradas (00-informe-integridad.md §4).
- [x] T-06 Redactar este paquete SDD (00–05). *Verif:* archivos en `.specs/langfuse-f1-recuperacion-y-diseno/`.

## F3 — Spool sqlite (tras aprobación)

- [ ] T-07 Prueba: schema + pragmas + apertura por home (dos homes → dos archivos, sin estado global). `pytest tests/test_fleet_spool.py -q` — skills: `python-testing-patterns`.
- [ ] T-08 Prueba: insert + ACK por evento (batch con `errors` → solo ids no-reintentables a `done`). `pytest tests/test_fleet_spool.py::test_ack -q`.
- [ ] T-09 Prueba: overflow acotado reject-new + `lost_overflow`; undersize event descartado con `lost_oversize`. `pytest -k overflow -q`.
- [ ] T-10 Prueba: caducidad 24 h → `expired` + purga + contador. `pytest -k expiry -q`.
- [ ] T-11 Prueba: crash recovery — `in_flight` huérfano → `pending` + contador `recovered`. `pytest -k recovery -q`.
- [ ] T-12 Prueba: reintento bounded (backoff, 5 intentos, `lost_attempts`) con transport bloqueado (patrón del contrato SDK existente). `pytest -k retry -q`.
- [ ] T-13 Prueba: cierre bounded — deadline 2 s, lo pendiente persiste. `pytest -k shutdown -q`.
- [ ] T-14 Implementar `langfuse-fleet/` (copia de `langfuse/` + módulo `fleet_spool.py` con las políticas de 02-design.md). *Depende:* T-07..T-13 en RED. *Verif:* suite completa GREEN + contrato SDK.
- [ ] T-15 Prueba de privacidad: ningún body en sqlite contiene claves fuera de `_METADATA_KEYS` ni valores con prefijos de secret (`_SECRET_PREFIX`). `pytest -k privacy -q` — skills: `code-review-and-quality`.
- [ ] T-16 Prueba multiplexing: cliente y spool keyeados por `hermes_home_key()`; credenciales solo vía `agent.secret_scope.get_secret` (sin `os.environ`). `pytest -k multiplex -q` — skills: `hermes-agent`.
- [ ] T-17 Manifest `plugin.yaml` `name: langfuse-fleet` + pyproject `hermes-langfuse-fleet` con pin `langfuse==4.15.2`. *Verif:* `hermes plugins validate` sobre el directorio nuevo (solo validación estática, sin instalar en perfiles operativos).

## F4 — Canario (autorizaciones separadas, fuera de esta tarea)

- [ ] T-18 Entorno canario aislado (perfil dedicado), credenciales por secret_scope, plugin oficial OFF solo en ese home.
- [ ] T-19 Observación 24 h con job read-only de contadores (asignación Vigía), reporte con series temporizadas. *Parcial 2026-11-03:* mecanismo en `langfuse-fleet/observability/` (sampler + productor acotado + drill + manifiesto) con pre-flight real y su ventana archivada en `f4_archive/preflight-20261103/`; no queda ventana abierta, el ciclo de 24 h lo programa Vigía bajo autorización. *Corrección tras revisión Heimdall (2026-11-03):* presupuesto del productor coherente con lo que mide (96 ciclos × 3 llamadas = 288 ≤ `max_total_events` 384, y `validate_manifest` rechaza lo demás) y criterio `no_stuck_rows` medido en el spool en lugar del vacío `zero_blocked`; suite `219 passed, 2 skipped`. *Corrección tras revisión 2 de Heimdall (2026-11-03):* `no_stuck_rows` dejó de contar toda fila `in_flight` de primer intento como atascada (`claim_batch` no sella el claim y `enqueue` deja `next_attempt_at=0`): la edad sale ahora de la última marca de ciclo de vida de la fila (`_claim_age`), con `in_flight_rows`/`in_flight_max_age_s` como evidencia; `fleet_spool` operativo intacto; suite `222 passed, 2 skipped` (42 tests F4), drill `ok: true`, sin ventana abierta.
- [ ] T-20 Informe de canario y decisión de extensión. *Gate:* aprobación explícita de Jonathan.
