# 01 — Especificación (F2: adaptador Langfuse de flota durable)

Estado: BORRADOR para revisión (gate heimdall). Fase F1 = este paquete de documentos + repo recuperado. La implementación (F3+) está bloqueada hasta aprobación.

## 1. Intención de negocio

Observabilidad de flota NeuralCrew sobre el Langfuse self-hosted ya existente en PROD (`100.73.30.29:3010`, Langfuse 2.95.11, contenedor `langfuse/langfuse:2`): trazas de metadatos por turno/llamada/herramienta, entregadas con la menor pérdida posible, sin bloquear turnos y sin tocar el core de Hermes.

## 2. Alcance

**Dentro:** plugin independiente recuperado (repo `/opt/hermes-official-plugins`, commits `e52545c` → `de4c12af` sobre base oficial `c2a95963`); diseño del transport durable; canario de METADATOS aislado; pruebas; observación 24 h.

**Fuera (no-goals explícitos):**
- Despliegue en agentes operativos DEV/PROD; restart de gateways; cambios al core de Hermes.
- Push/PR/merge del repo recuperado (publicación sigue desautorizada).
- Migración de Langfuse, servicios nuevos, APIs pagas, envío de contenido/prompts/secrets.
- Promesa exactly-once o cero pérdidas (imposible con este server y sin ACK transaccional: se diseña at-least-once con IDs estables y contadores de pérdida auditables).

## 3. Decisiones ya tomadas (no se re-deciden abajo)

1. **Nombre de distribución independiente:** plugin `langfuse-fleet` (manifest `name: langfuse-fleet`), distinto del oficial `langfuse` para que el later-wins del `PluginManager` y cualquier reinstalación del catálogo nunca lo sobrescriban ni lo sombreen.
2. **Fuente versionada reproducible:** este repo; pin de SDK `langfuse==4.15.2` (ya declarado en `pyproject.toml`); pin de origen = base oficial `c2a95963c442f3f23e4bc6fe0da898e1dd1fb63d` + commits locales `e52545c16942f33398c371a75a0cea5ffdacec4f` y `de4c12af69e119e2fdde6d744fc8f19bb9e66cef`.
3. **Transport:** metadata-only REST v2 (`legacy_ingestion.py`), extendido con cola persistente sqlite (stdlib) — ver 02-design.md.
4. **Desactivación/alias del plugin oficial** (`langfuse` oficial OFF, `langfuse-fleet` ON, un solo emisor): SOLO dentro del entorno canario aislado futuro. Jamás en perfiles operativos.
5. **Server destino del canario:** el Langfuse PROD existente, con credenciales de proyecto dedicadas inyectadas por `agent.secret_scope` (sin `os.environ` fallback en multiplexing).

## 4. Criterios de aceptación (Given/When/Then)

- **AC1 Cola durable:** Given un evento encolado, When el proceso muere antes de entregarlo, Then al reiniciar el evento se re-entrega (sqlite) y el contador `recovered` lo refleja.
- **AC2 ACK verificado:** Given un batch aceptado con `errors` por evento, Then solo los eventos sin error (o reintentables 429/5xx) salen de `pending`; el evento se marca `done` únicamente cuando se verifica aceptación.
- **AC3 Acotado:** Given saturación sostenida, When la cola alcanza el límite (filas o bytes), Then se descartan eventos NUEVOS con contador `lost_overflow` y el productor nunca se bloquea más de la escritura sqlite.
- **AC4 Caducidad:** Given un evento con edad > 24 h, Then se marca `expired` y se purga; el contador lo reporta.
- **AC5 Reintentos bounded:** Given fallo de red/5xx, Then reintento con backoff exponencial acotado (máx. 5 intentos por evento, timeout 1.5 s por request, `max_retries=0`); agotados → contador `lost_attempts`.
- **AC6 Cierre bounded:** Given `atexit`/`on_session_end`, Then el drain espera un deadline (2 s) y termina; lo no entregado queda persistido para el próximo arranque.
- **AC7 Privacidad:** Given cualquier evento, Then la metadata pasa por la allowlist (`_safe_metadata`) ANTES de escribirse al spool sqlite; ni DB ni logs contienen prompts/contenido/secrets.
- **AC8 Multiplexing:** Given un proceso que sirve varios perfiles/homes, Then el cliente y su spool se keyean por home entregado por llamada (`hermes_home_key()`), y las credenciales salen de `secret_scope.get_secret` sin lectura de `os.environ`.
- **AC9 Coste:** Given usage de Hermes, Then el evento lleva usage (input/output/cache) y el coste ESTIMADO queda distinguible del real: en Langfuse v2 por `usage` del observation + documentación de la limitación (el adaptador descarta `costDetails`; sin promesa de coste real server-side).
- **AC10 Auditabilidad:** Given un periodo de 24 h, Then un job de observación lee los contadores del spool (read-only) y reporta entregados/perdidos por causa; huecos de muestreo quedan visibles por timestamp.
- **AC11 Turnos:** Given cualquier latencia o caída del server, Then la durabilidad nunca bloquea el turno (entrega 100 % en worker daemon; productor solo hace insert sqlite rápido).

## 5. Edge cases y modos de fallo

- Server caído / latencia alta → backoff, eventos persistidos, contadores crecen, turnos intactos.
- Proceso SIGKILL → eventos `in_flight` huérfanos se recuperan a `pending` en el arranque (lease por PID/timestamp).
- Disco del spool lleno → insert falla → evento se descarta con contador `lost_disk` (fail-open, jamás bloquea el turno).
- Reloj de server vs local → se envían timestamps ISO-8601 propios; Langfuse usa el del cliente (aceptable, documentado).
- Duplicados post-retry (request aceptado, respuesta perdida) → mitigado por IDs estables (Langfuse fusiona por id); sin garantía exactly-once, documentado.
