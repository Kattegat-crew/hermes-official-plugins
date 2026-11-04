# Staging canario — `langfuse-fleet`

Runbook reproducible para probar la variante fleet **en un entorno aislado**, sin tocar perfiles
operativos, gateways, el plugin oficial instalado ni Langfuse PROD. Nada de este documento se ha
ejecutado contra un endpoint vivo: el canario real es la fase F3 y requiere autorización explícita.

## Precondiciones

- Runtime Hermes con `langfuse==4.15.2` disponible en el entorno PM/uv (`uv run --offline`).
- Un `HERMES_HOME` **dedicado** (nunca el de un perfil operativo).
- Credenciales del proyecto Langfuse obtenidas por el mecanismo `agent.secret_scope`: el plugin las
  lee con `get_secret` y **falla cerrado** si el scope no existe. No se pegan claves en el chat, en
  logs ni en este repo.
- Un único exportador en ese home: el plugin oficial `langfuse` no debe estar habilitado allí.

## 1. Verificación sin red (siempre)

```bash
cd /opt/hermes-official-plugins/langfuse-fleet
PYTHONPATH=/opt/hermes-agent /usr/bin/python3.12 -m pytest -q
UV_CACHE_DIR=/opt/hermes-agent-data/cache/uv uv run --offline --no-project \
  --with langfuse==4.15.2 --python 3.14 tests/test_legacy_sdk_contract.py
```

Esperado: `180 passed, 2 skipped` y `SDK4 serializer, retry, split-batch, and blocked-transport
contracts passed`. Ambas pruebas usan transporte mockeado: no hay red ni credenciales reales.

## 2. Instalación en el home aislado (sin activar)

```bash
export CANARY_HOME=/tmp/langfuse-fleet-canary
mkdir -p "$CANARY_HOME/plugins"
cp -r /opt/hermes-official-plugins/langfuse-fleet "$CANARY_HOME/plugins/langfuse-fleet"
HERMES_HOME="$CANARY_HOME" hermes plugins validate langfuse-fleet
```

No se habilita el plugin en ningún perfil operativo. El spool vive en
`$CANARY_HOME/plugins/langfuse-fleet/spool.db`.

## 3. Inspección del spool (antes de cualquier envío)

```bash
sqlite3 "$CANARY_HOME/plugins/langfuse-fleet/spool.db" \
  "SELECT state, COUNT(*) FROM events GROUP BY state; SELECT key, value FROM counters;"
```

Contadores esperados antes del canario: `delivered=0`, sin `lost_*`. Tras un canario autorizado, cada
evento debe pasar a `done` **sólo** si el transporte devolvió aceptación; los `429/5xx` quedan
`pending` con `next_attempt_at` futuro.

## 4. Canario autorizado (fase F3, no ejecutada aquí)

1. Activar el plugin sólo en `$CANARY_HOME` y arrancar un proceso Hermes aislado con ese home.
2. Emitir un turno sintético determinista, sin datos de cliente.
3. Verificar aceptación HTTP y **después** leer de vuelta la traza por API/DB read-only del servidor;
   aceptación HTTP no es prueba de traza consultable.
4. Reiniciar sólo el proceso canario para ejercitar recovery del spool e idempotencia por ids
   estables.
5. Parar únicamente el exportador canario para el rollback; preservar el spool para diagnóstico.

## Límites conocidos

- **At-least-once**, no exactly-once: los ids de evento son estables y Langfuse de-duplica; una
  entrega repetida es posible tras un crash entre el ACK y el `done`.
- **Aceptación ≠ traza visible:** el spool sólo registra lo que el transporte aceptó. La
  materialización en la UI/DB de Langfuse es del servidor.
- **Costo estimado, no real:** el plugin envía `cost_details` estimados desde el pricing de Hermes; el
  costo facturado del proveedor no se conoce y no se afirma.
- **`costDetails` legacy:** el evento de ingestión legacy no transporta el costo como campo de primer
  nivel; no prometer coste visible en Langfuse sin la prueba de readback de F3.
- **Un writer por home:** el spool es sqlite local por perfil. Varios procesos sobre el **mismo** home
  compiten por el archivo (WAL + leases), pero el diseño soporta un exportador por home.
- **Spool acotado:** al agotar `MAX_ROWS`/`MAX_PENDING_BYTES` o con un evento mayor que el límite, el
  evento se descarta en el productor y sólo se incrementa un contador (nunca se bloquea el turno).
- **`EXPIRY_AGE_S=24h`** retira eventos que nunca llegaron a aceptarse: no hay reintento infinito.
- **Sin observación 24h todavía:** la ventana pertenece a F4 y no se ha iniciado.
