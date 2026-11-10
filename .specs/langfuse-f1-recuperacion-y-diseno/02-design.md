# 02 — Diseño Técnico

Base de código: `langfuse/` en este repo (`__init__.py` 1240 líneas, `legacy_ingestion.py` 318 líneas). El diseño extiende, no reescribe: la superficie SDK-compatible (`LegacyIngestionClient` / `LegacyObservation`) ya pasa la suite 155/2 y el contrato SDK.

## 1. Arquitectura

```
turno (hook post_llm_call/post_tool_call/…)
  │  metadata dict (sin procesar)
  ▼
[allowlist _safe_metadata]  ← filtro ANTES de todo spool (AC7)
  │  evento {id estable, type, body}
  ▼
[FleetSpool: sqlite3 WAL]   ← única escritura síncrona del productor (rápida)
  │  notify
  ▼
[_DeliveryWorker daemon, existente]
  │  batch ≤128 evt / ≤512 KiB, request timeout 1.5 s, max_retries=0
  ▼
Langfuse v2 REST /api/public/ingestion  ──ACK por evento──▶ done
```

El cambio estructural respecto del código actual: la cola en memoria (`deque` del `_DeliveryWorker`) pasa a ser **respaldada por sqlite**, de modo que lo no entregado sobrevive al proceso (AC1/AC6).

## 2. Esquema del spool (sqlite stdlib, un archivo por home)

Ruta: `<hermes_home>/plugins/langfuse-fleet/spool.db` — keyeada por el home que entrega cada llamada (`hermes_home_key()`); nunca se cachea «el» home (regla multiplexing HX002/HX012).

```sql
CREATE TABLE events (
  id            TEXT PRIMARY KEY,   -- envelope id estable (uuid4 hex del evento)
  trace_id      TEXT NOT NULL,
  type          TEXT NOT NULL,      -- trace-create | generation-create | span-update | …
  body_json     TEXT NOT NULL,      -- ya filtrado por allowlist
  bytes         INTEGER NOT NULL,
  created_at    REAL NOT NULL,      -- epoch s
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  state         TEXT NOT NULL DEFAULT 'pending',  -- pending|in_flight|done|expired|lost
  owner         TEXT                -- pid+boot_id del in-flight (lease para crash recovery)
);
CREATE TABLE counters (
  key   TEXT PRIMARY KEY,           -- delivered | lost_overflow | lost_oversize |
                                    -- lost_attempts | expired | recovered | batches_acked
  value INTEGER NOT NULL
);
```

Pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=1000`.

## 3. Políticas

- **Acotado:** máx. 4096 filas y 8 MiB pendientes. Al alcanzar el límite se rechazan eventos NUEVOS (`enqueue` devuelve False, warning count-only) y se incrementa `lost_overflow`. Lo ya aceptado nunca se expulsa (AC3).
- **ACK por evento:** al responder el batch sin `errors`, todo el lote → `done`. Con `errors`, solo los ids con error salen del estado pendiente y reintentan si 429/500/502/503/504; el resto → `done`. La aceptación es la única transición a `done` (AC2).
- **Reintentos:** `attempts+1` por fallo; `next_attempt_at = now + min(5·2^attempts, 900)` s; máx. 5 intentos → `state='lost'`, contador `lost_attempts`. Request timeout 1.5 s, `max_retries=0` (sin cambio del contrato SDK probado).
- **Caducidad:** `created_at` con edad > 24 h → `expired` + purga, en el arranque y cada 10 min dentro del worker (AC4).
- **Cierre bounded:** `flush_all(deadline=2 s)` en `atexit` (ya existe `flush_legacy_delivery`; se respeta su semántica y se documenta que NO es una cota de apagado). Tras el deadline, los `pending/in_flight` quedan en sqlite para el próximo arranque (AC6).
- **Crash recovery:** al construir el cliente, `UPDATE events SET state='pending', owner=NULL WHERE state='in_flight'` (lease huérfano) y contador `recovered += n` (AC1).
- **Contadores:** incrementos transaccionales junto a la transición de estado; lectura read-only para el monitor (AC10).

## 4. Coste estimado vs real

- `usage_details` (input/output/cache) se envía por generación como hoy.
- El coste ESTIMADO de Hermes (`agent.usage_pricing`) **no** viaja como `costDetails`: el adaptador v2 lo descarta y Langfuse 2.95.11 no garantiza coste server-side para modelos no catalogados. Se documenta como limitación explícita (AC9): en el dashboard, el coste mostrado —si existe— es el calculado por Langfuse; los estimados de Hermes quedan fuera de este canal hasta un cambio explícito de alcance. Sin promesa de exactitud de coste en Langfuse.

## 5. Distribución y canario

- Directorio del plugin nuevo: `langfuse-fleet/` en este repo (copia versionada de `langfuse/` + spool), manifest `name: langfuse-fleet`, `requires_env` idéntico (pk/sk secret:true), `optional_env: HERMES_LANGFUSE_BASE_URL`.
- Instalación canario futura: `$HERMES_HOME/plugins/langfuse-fleet/` en un perfil aislado (no default, no operativo); en ESE home: `plugins.enabled` sin el oficial `langfuse` (alias único de emisor, decisión #4 del spec).
- El plugin instalado actual `/opt/hermes-agent-data/plugins/langfuse` (oficial, sin .git, pin `c2a95963`) queda INTACTO: nada de este diseño toca homes operativos.

## 6. Supervisión 24 h (sin worker dormido ni monitorización fingida)

- El adaptador NO mantiene hilos vivos entre entregas: el daemon worker es on-demand (igual que hoy) y muere con el proceso; la durabilidad vive en sqlite, no en memoria residente.
- Un cron de Hermes (asignado a Vigía, read-only) consulta cada 15 min los `counters` y filas `pending` del spool del canario y registra el delta con timestamp. El monitor solo reporta lo que lee de la DB; si el monitor falla, el hueco es visible en la serie de timestamps (no se inventan muestras). Ninguna credencial viaja en los reportes.

## 7. Trade-offs y riesgos

| Decisión | Alternativa | Riesgo aceptado |
|---|---|---|
| sqlite por home (stdlib) | Redis/cola externa | dependencia nueva prohibida por alcance; sqlite basta al volumen de metadatos |
| Reject-new en overflow | Evict-oldest | se pierde frescura, no lo ya aceptado; contable |
| at-least-once + IDs estables | exactly-once | duplicados posibles tras retry; Langfuse fusiona por id |
| Metadata-only | contenido completo | cero riesgo de PII en DB/errores; limitación documentada |

## 8. Rollback

Ver `04-rollback.md`. Ninguna superficie del core, del plugin oficial ni de gateways es modificada; el rollback es des-instalar el plugin nuevo en el canario y borrar su home de spool.
