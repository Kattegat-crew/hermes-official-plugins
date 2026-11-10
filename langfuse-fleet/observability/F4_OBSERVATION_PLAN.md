# F4 — observación viva de 24 h (langfuse-fleet)

Estado: mecanismo construido, probado y con pre-flight real ejecutado. **No hay ninguna ventana
de 24 h abierta**: la de preflight se archivó (`f4_archive/preflight-20261103/`) y el ciclo
continuo que la completa lo programa Vigía con autorización. El veredicto de ese preflight es
`F4 FAIL` a propósito (5 muestras, no 24 h), y así debe leerse.

## 1. Qué se observa y qué no se toca

Sistema bajo prueba: el plugin `langfuse-fleet` en su copia aislada
`/opt/hermes-agent-data/fleet-canary` (transporte `legacy`, captura `metadata`), entregando a
Langfuse PROD vía `http://100.73.30.29:3010`.

Invariantes:

1. Ningún exportador operativo (Vigía / default / cualquier perfil vivo) se reinicia, reconfigura
   ni se toca. F4 escribe sólo en el home del canario.
2. Sin contenido: la captura sigue en `metadata` y el productor emite ids sintéticos. La
   readback valida allowlist y busca tokens de fuga (`FAKE`, `canary prompt`,
   `synthetic tool result`).
3. Sin llamadas a modelo por defecto: el productor sintético hace cero llamadas. El turno real
   está en el manifiesto con provider+model fijados, `fallback_providers: []` y
   `model_calls_allowed_until_authorized: 0`; ejecutarlo requiere provisión de auth y
   autorización explícita (ver §6).
4. Entrega at-least-once: el drill demuestra recuperación tras SIGKILL, no exactly-once.
5. Un veredicto decorativo es imposible: `evaluate()` es un AND de 13 criterios duros y sólo
   compara deltas contra la muestra baseline (§4).

## 2. Componentes (`observability/`)

| Archivo | Rol |
|---|---|
| `f4_manifest.json` | Configuración sin auth: ventana, cadencia, sesiones, allowlist, umbrales, topes del productor, endpoint, turno real. |
| `f4_observe.py` | Sampler + report. `--init/--rotate`, `--sample`, `--report`, `--status`. Spool en `mode=ro`. |
| `f4_producer.py` | Productor acotado, cero LLM, hooks reales del plugin, heartbeat, lock, SIGTERM. `--once`, `--cycles`, `--interval`, `--mock`, `--status`, `--stop`. |
| `f4_recovery_drill.py` | Drill de recuperación a mitad de ventana con SIGKILL real, sin red. |
| `f3_canary_driver.py`, `f3_readback.py` | Originales F3 verbatim (ver `PROVENANCE.md`). |

## 3. Contrato de una muestra

```
ts_utc, window_id, elapsed_h, baseline,
spool{counters(delivered, recovered, batches_acked, expired, rejected, lost_*),
      events_by_state, rows_total, attempts_sum,
      in_flight_rows, in_flight_max_age_s, stuck_in_flight, stuck_after_s},
readback{role: {http_status, n_traces, duplicate_ids, metadata_within_allowlist, content_leak_traces}},
producer{present, cycles, last_cycle_utc, events_emitted, stopped}
```

`emitted_total = rows_total + counters.expired` se deriva en el report, no en el plugin: no
existe un contador "enqueued" y no se inventa uno. `in_flight_rows` cuenta las filas que la
muestra ve en vuelo en el spool (información de auditoría, no un criterio; una ventana sana tiene
filas en vuelo en casi todos los momentos). `stuck_in_flight` sí se mide en el spool
y es un criterio: filas `in_flight` cuya última marca de ciclo de vida es más vieja que
`producer.stuck_lease_s`. Como `claim_batch` **no sella la hora del claim** y `enqueue` deja
`next_attempt_at` en su default `0`, no existe un timestamp de claim: se usa el último mark que
precede al claim (`created_at` mientras `attempts == 0`, el `next_attempt_at` del reprogramo de
`settle_batch` después de un fallo). Es un **límite superior** del tiempo en vuelo, por lo que
también marca una fila que envejeció en la cola más de una lease antes de ser reclamada: eso es
retraso de entrega y no una ventana sana. `in_flight_max_age_s` deja ese número visible en cada
muestra para auditarlo.

## 4. Criterios del veredicto (`f4_observe.evaluate`)

| Criterio | Regla |
|---|---|
| `window_elapsed` | reloj real ≥ 24 h desde `start_at_epoch` |
| `samples_complete` | ≥ 97 muestras (baseline + 96 intervalos) |
| `no_sampling_gaps` | ningún intervalo > 31 min |
| `no_zero_emission_intervals` | cada intervalo aporta ≥ 1 evento |
| `emissions_observed` | delta emitido > 0 |
| `delivery_ratio` | entregado/emitido ≥ 0.95 |
| `no_stuck_rows` | 0 filas `in_flight` con última marca de ciclo de vida más vieja que `producer.stuck_lease_s` (`created_at` en el primer intento, `next_attempt_at` tras un fallo de entrega; sin timestamp de claim, porque `claim_batch` no lo sella) |
| `no_lost_counters` | delta de `lost_*` = 0 |
| `no_counter_resets` | ningún contador monótono decrece (si el spool se reemplaza, FAIL) |
| `producer_heartbeat` | heartbeat con ≥ 2 ciclos y antigüedad ≤ 31 min |
| `readback_clean` | 0 errores, 0 ids duplicados, 0 fugas, metadata ⊆ allowlist |
| `readback_covers_sessions` | todas las sesiones `required` leídas |
| `active_export_observed` | el conteo de trazas de la sesión creció (un readback viejo no cuenta) |

`F4 PASS` sólo si los 13 pasan. Cualquier FAIL imprime `failed_criteria` y sale con código 3.

## 5. Operación

```
cd /opt/hermes-agent-data/repos/hermes-official-plugins/langfuse-fleet/observability
H=/opt/hermes-agent-data/fleet-canary

# ventana (idempotente: se niega si ya existe; --rotate archiva antes de abrir otra)
python3 f4_observe.py --init --home $H

# ciclo continuo (bounded: max_cycles=96 × events_per_cycle=3 → max_total_events=384; SIGTERM limpio)
python3 f4_producer.py --home $H

# muestreo cada 15 min + veredicto al cierre
python3 f4_observe.py --sample --home $H
python3 f4_observe.py --report --home $H
```

`max_total_events` cuenta **llamadas a hooks**, no ciclos: cada turno sintético hace 3
(`pre_api_request`, `post_api_request`, `on_session_finalize`). Por eso `validate_manifest`
rechaza cualquier manifiesto con `max_total_events < max_cycles * events_per_cycle`; con un
presupuesto corto (200 con 96×3=288) la ventana se cortaba a los 67 ciclos (~16.75 h) y el
veredicto fallaba `no_zero_emission_intervals` y `producer_heartbeat` sobre un sistema sano.
`f4_producer.py` valida el manifiesto al arrancar y se niega a correr con uno incoherente.

`--init` nunca sobreescribe historial: con `--rotate` copia `f4_observation_state.json` y
`f4_samples.jsonl` a `f4_archive/<window_id>/` con su `archive_manifest.json` y recién
entonces abre una ventana nueva con `window_id` distinto.

Salvaguardas ya cableadas: `f4_observe._default_home()` ignora `HERMES_HOME` (un shell de
perfil operativo no puede secuestrar el muestreo) y `f4_producer.assert_isolated()` se niega
a emitir si el home es el `HERMES_HOME` del shell o si no tiene copia aislada del plugin.

## 6. Pre-flight ejecutado (evidencia real, 2026-11-03)

| Paso | Resultado |
|---|---|
| Suite del plugin | `219 passed, 2 skipped` (39 en `tests/test_f4_observability.py`), dos corridas — corrida del preflight; el estado vivo tras la ronda 3 está en la nota de abajo |
| Drill de recuperación | `ok: true` — 3 eventos encolados, SIGKILL (`returncode -9`), `recovered: 3`, `delivered: 3`, re-ack `0`, `lost_*` = 0 |
| Readback F3 (control) | sesión `fleet-f3-canary`: 4 trazas, `metadata_within_allowlist: true`, 0 fugas, 0 duplicados |
| Ventana de preflight | `window_id 20261103T162030Z-5bdb94c4`, baseline `delivered 7`, `rows_total 7`, `fleet-f4-live` 0 trazas; 5 muestras |
| Productor `--once` ×2 | run 1: +5 filas, `delivered 7→10`, traza 0→1; run 2: `recovered 21→23`, `delivered 10→12`, traza 1→2 |
| Report | `F4 FAIL` con `window_elapsed, samples_complete, no_zero_emission_intervals, producer_heartbeat, delivery_ratio` (correcto: 5 muestras, no 24 h) |
| Cierre | ventana de preflight archivada en `f4_archive/preflight-20261103/` (`f4_observation_state.json`, `f4_samples.jsonl`, `f4_producer_heartbeat.json`, `report.json`). **No queda ninguna ventana 24 h abierta.** |

Estado vivo de la suite (tras la ronda 3 de revisión, 2026-11-03): **`222 passed, 2 skipped`**
y **42** tests en `tests/test_f4_observability.py`, dos corridas con el mismo resultado. La
corrida del preflight registrada arriba fue `219 passed, 2 skipped` (39 tests F4).

El run 1 dejó 2 filas `in_flight` (worker murió con el proceso): el run 2 las recuperó y
entregó. Es exactamente el comportamiento at-least-once que el drill formaliza.

### 6.1 Correcciones tras la revisión 1 (Heimdall, changes requested)

| Hallazgo | Corrección | Prueba (RED → GREEN) |
|---|---|---|
| `max_total_events=200` < 96×3=288: la ventana se cortaba a los 67 ciclos (~16.75 h) y fallaba en falso | `events_per_cycle: 3`, `max_total_events: 384`; `validate_manifest` rechaza presupuestos que no cubren `max_cycles*events_per_cycle`; `f4_producer.load_manifest` se niega a arrancar con uno inválido | `test_shipped_manifest_budget_covers_the_full_window`, `test_manifest_rejects_unsafe_verdict_shapes[<lambda>-max_total_events1]`, `test_producer_completes_a_full_window_without_hitting_the_event_cap` (`assert 67 == 96` → 96 ciclos, sin `limit_reason`) |
| `zero_blocked` era decorativo: `fleet_spool` nunca escribe el estado `blocked` | Sustituido por `no_stuck_rows`, con señal real medida en el spool (`stuck_in_flight`: filas `in_flight` con claim más viejo que `stuck_lease_s`) | `test_spool_snapshot_detects_a_stalled_in_flight_lease`, `test_stalled_or_lost_rows_fail[<lambda>-no_stuck_rows]` |
| Documentación desalineada (213 passed, presupuesto 200) | §5 y §6 actualizados a la corrida viva | `219 passed, 2 skipped` (dos corridas) y `39` tests F4 |

Esta ronda no abrió ventana nueva ni envió nada a Langfuse: sólo repitió pruebas locales, el
drill de recuperación y la validación del manifiesto. La evidencia de preflight de arriba es la
histórica y no se reescribió.

### 6.2 Correcciones tras la revisión 2 (Heimdall, changes requested)

| Hallazgo | Corrección | Prueba (RED → GREEN) |
|---|---|---|
| `no_stuck_rows` no medía la edad del claim: `enqueue` deja `next_attempt_at=0` y `claim_batch` no lo sella, así que toda fila `in_flight` de primer intento contaba como atascada al instante (falso FAIL en cualquier ventana sana que muestre durante un POST) | `spool_snapshot` calcula la edad de cada fila `in_flight` desde su última marca de ciclo de vida que precede al claim (`_claim_age`: `created_at` con `attempts == 0`, `next_attempt_at` después de un fallo), publica `in_flight_rows` y `in_flight_max_age_s`, y sólo cuenta como atascada lo que supera `stuck_lease_s`. `fleet_spool` operativo **no se tocó** | `test_a_fresh_claim_is_not_stuck` (`stuck_in_flight` 5 → 0 en un claim de milisegundos), `test_spool_snapshot_ages_a_stalled_retried_claim`, `test_a_row_in_flight_past_the_lease_is_stuck` |
| Falso PASS en la dirección opuesta: una ventana sana con entregas en el aire | `in_flight_rows` es informativo, no criterio; sólo `stuck_in_flight > 0` falla | `test_transient_in_flight_rows_do_not_fail_a_healthy_window` (97 muestras con 12 filas en vuelo en varias → `F4 PASS`, `in_flight_rows_max` = 12) |

RED → GREEN medido sobre el mismo escenario que encontró el reviewer (5 filas recién reclamadas,
`stuck_after_s=3600`):

```
antes   rows: [('fresh-0', 0, 1793735810.3, 0.0, 'in_flight'), ...]  stuck_in_flight: 5
después rows: [('fresh-0', 0, 1793735990.5, 0.0, 'in_flight'), ...]  stuck_in_flight: 0
        in_flight_rows: 5, in_flight_max_age_s: 0.001
```

Esta ronda tampoco abrió ventana ni envió nada a Langfuse: sólo pruebas locales (suite
`222 passed, 2 skipped` ×2, F4 `42 passed` ×2, drill `ok: true`) y el manifiesto sigue sin
auth. El criterio `no_stuck_rows` sigue capaz de fallar de punta a punta
(`test_stalled_or_lost_rows_fail[<lambda>-no_stuck_rows]`).

## 7. Decisiones abiertas (no las toma este run)

1. **Programación de las 24 h** — Vigía debe correr `f4_producer.py` una vez y `--sample`
   cada 15 min durante 24 h, y `--report` al cierre. Sin eso la ventana no avanza.
2. **Turno real** (`real_turn`) — el pin es `openai-codex` + `gpt-6.1-sol` (catálogo vivo,
   `api_mode: codex_responses`, facturación `subscription_included`). Falta provisionar la
   credencial existente en el home aislado: es decisión de Jonathan, no de este run. No se
   agregó ningún fallback.
3. **Arranque del plugin en PROD** (`python -m hermes_cli.app`) — el exportador legacy requiere
   un arranque real del proceso que lo aloja para reanudar entrega continua; eso es cambio
   operativo y necesita aprobación aparte.

## 8. Rollback

1. `python3 f4_producer.py --stop --home $H` (mata sólo el PID del lock).
2. Archivar `f4_observation_state.json`, `f4_samples.jsonl` y copia del `spool.db` en
   `f4_archive/`.
3. Las trazas sintéticas en PROD quedan bajo `fleet-f4-live`, sin tocar datos de clientes.
