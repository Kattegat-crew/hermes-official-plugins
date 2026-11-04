# 05 — Matriz de Pruebas

Intérpretes de referencia: suite pytest con `/usr/bin/python3.12` + `PYTHONPATH=/opt/hermes-agent` (pytest 9.1.1); contrato SDK con el venv PM (python 3.14, SDK `langfuse==4.15.2`). Sin red real: transport bloqueado/mockeado igual que el contrato SDK existente.

## A. Regresión del código heredado (ejecutado en F1)

| # | Suite | Comando | Esperado | Estado F1 |
|---|---|---|---|---|
| A1 | Plugin completo | `cd langfuse && PYTHONPATH=/opt/hermes-agent /usr/bin/python3.12 -m pytest -q` | 155 passed, 2 skipped | ✅ reproducido 2026-11-02 |
| A2 | Contrato SDK standalone | `cd langfuse && python3 tests/test_legacy_sdk_contract.py` | exit 0 («SDK4 … contracts passed») | ✅ reproducido 2026-11-02 |
| A3 | Integridad repo | `git fsck --full` + `git status --porcelain` | sin errores; 0 entradas | ✅ |

## B. Nuevas pruebas de spool/durabilidad (F3, TDD)

| # | Prueba | AC | Verificación |
|---|---|---|---|
| B1 | Apertura por home; dos homes → dos spools aislados | AC8 | `pytest tests/test_fleet_spool.py -q` |
| B2 | ACK por evento: batch con `errors` → solo ids no-reintentables a `done`; reintentables (429/5xx) vuelven a `pending` | AC2 | `pytest -k ack -q` |
| B3 | Persistencia entre reinicios de proceso (spawner subprocess con SIGKILL) | AC1 | `pytest -k persistence -q` |
| B4 | Crash recovery de `in_flight` huérfanos + contador `recovered` | AC1 | `pytest -k recovery -q` |
| B5 | Overflow reject-new + `lost_overflow`; oversize + `lost_oversize`; productor nunca espera red | AC3, AC11 | `pytest -k overflow -q` |
| B6 | Caducidad 24 h → `expired` + purga | AC4 | `pytest -k expiry -q` |
| B7 | Backoff exponencial acotado; 5 intentos → `lost_attempts`; timeout 1.5 s por request | AC5 | `pytest -k retry -q` |
| B8 | Cierre bounded: drain con deadline; pendiente sobrevive | AC6 | `pytest -k shutdown -q` |
| B9 | Privacidad: allowlist antes de spool; sin secret-patterns en body_json ni en logs | AC7 | `pytest -k privacy -q` |
| B10 | Contadores: incrementos atómicos, lectura read-only | AC10 | `pytest -k counters -q` |
| B11 | Durabilidad no bloquea el turno: latencia de insert < cota y entrega 100 % async | AC11 | `pytest -k latency -q` (cota de pared ≥ 2 s, sync por Event) |

## C. Contratos estáticos (F3)

| # | Verificación | Comando |
|---|---|---|
| C1 | Manifest del plugin nuevo válido | `hermes plugins validate <dir>/langfuse-fleet` |
| C2 | Pin SDK reproducible | grep `langfuse==4.15.2` en `langfuse-fleet/pyproject.toml` |
| C3 | Nombre no colisiona con catálogo ni removed | comparación contra `plugin-catalog/` del host (solo lectura) |

## D. Observación 24 h (F4)

- Serie temporizada de `counters` (delta cada 15 min, job read-only de Vigía) + filas `pending` residuales al cierre.
- Criterios de éxito del canario (propuesta, sujeta a aprobación): 0 bloqueos de turno; `lost_*` documentados y explicados; entregados ≥ 95 % de eventos emitidos; huecos de muestreo del monitor justificados.

## Nota de límites

Las pruebas B mockean el server: el comportamiento real de Langfuse 2.95.11 (errores por id, dedup por id) se valida solo parcialmente con el contrato SDK y se confirma en el canario. No hay prueba de red en F1/F3 — el envío real de trazas está fuera del alcance autorizado de esta fase.
