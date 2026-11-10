# t_d72cd140 — Exportador único de Langfuse por home (decisión previa al despliegue F5)

Estado: **análisis cerrado, sin desplegar**. No se instaló, desplegó ni modificó ningún plugin,
config de perfil ni servidor. Todas las pruebas corrieron en HERMES_HOMEs desechables bajo
`kanban/workspaces/t_d72cd140/fix/homes/` (symlinks al árbol F5 o al v2.0.0 real, sin escribir en
ninguno de los dos). Cifras medidas el **2026-11-03 entre 18:20 y 18:28 (-05)**.

---

## 0. Veredicto en una línea

Hay **un exportador por home** (12 homes: 11 perfiles + home raíz; `profiles/default` es el único que
habilita la clave sin tener el plugin) y **no entrega nada desde 2026-10-14**: emite OTLP contra un
servidor 2.95.11 que no tiene esa ruta (**404**, última línea de log hoy 18:28:02). La única variante
que entrega es **F5 + `HERMES_LANGFUSE_TRANSPORT=legacy`**: medida de punta a punta, **12/12 homes,
traza real legible en el servidor**.

---

## 1. Inventario por home (12 con Langfuse habilitado)

| Home | `plugins.enabled` hoy | Dir del plugin | Build | Registro del instalador | Spool |
|---|---|---|---|---|---|
| **root home** `/opt/hermes-agent-data` | 16 claves, incl. `langfuse` | `plugins/langfuse` | v2.0.0 | `catalog.name=langfuse`, pin `c2a9596` | — |
| `bragi` | `homeassistant`, `langfuse`, `observability/langfuse`, `spotify`, `typesafe-skill-router` | `plugins/langfuse` | v2.0.0 | ídem | — |
| `brokkr` | `langfuse`, `observability/langfuse`, `typesafe-skill-router` | `plugins/langfuse` **+** `plugins/langfuse-fleet` (solo `spool.db`) | v2.0.0 | ídem | 122 done / 7 lost (canary) |
| `comms` | ídem `brokkr` | `plugins/langfuse` | v2.0.0 | ídem | — |
| `freyja` | ídem `brokkr` | `plugins/langfuse` | v2.0.0 | ídem | — |
| `heimdall` | ídem `brokkr` | `plugins/langfuse` **+** `plugins/langfuse-fleet` (solo `spool.db`) | v2.0.0 | ídem | 16 done / 1 lost (canary) |
| `roshi`, `sindri`, `ullr` | ídem `brokkr` | `plugins/langfuse` | v2.0.0 | ídem | — |
| `hermodr`, `vili` | `observability/langfuse`, `typesafe-skill-router` | `plugins/langfuse` | v2.0.0 | ídem | — |
| **perfil `default`** `profiles/default` | 16 claves, incl. `langfuse` | **ninguno** | — | — | — |
| `vigia` | sin claves Langfuse | — | — | — | — |

Dos aclaraciones que la versión anterior mezclaba: **"12 homes" = 11 perfiles + el home raíz**
(`/opt/hermes-agent-data`), y el perfil `default` (`profiles/default`) es un home distinto del home raíz.
`profiles/default` habilita `langfuse` pero no tiene el directorio → **0 exportadores, en silencio**.

Datos crudos: `fix/out/homes.json`, `out/inventory.json`, `fix/show_provenance.py`.

**Corrección al hecho del ticket** ("9 perfiles habilitan a la vez standalone y core"): no hay dos
exportadores. `legacy_plugin_keys()` (`hermes_cli/left_core_migration.py:229`) hace que
`observability/langfuse` resuelva al **mismo** plugin, y el dir core
`/opt/hermes-agent/plugins/observability/langfuse/` **no tiene `plugin.yaml`** (solo `__init__.py`) → no
es un manifiesto cargable. Medido (M1): 1 manifiesto, 1 plugin, 11 hooks, **0 hooks duplicados**.

## 2. Cadena causal de la pérdida de trazas

1. `sys.path` real de los agentes: `PYTHONPATH=/opt/hermes-agent:<install-env>/venv/lib/python3.14/site-packages`.
2. Ese venv trae **langfuse 4.15.2** (`install_env_venv_site_packages`, inodo 5072069).
   **El store 2.x sí existe** y está duplicado en la ruta, no ausente: `/opt/data/lazy-packages` y
   `/opt/hermes-agent-data/lazy-packages` son **el mismo inodo 2671944**, con
   `langfuse-2.60.10.dist-info`. También hay un store 4.x aparte (`lazy-packages-langfuse4/py314`).
   Lo que decide es el **orden de `sys.path`**: con el `PYTHONPATH` real, el venv queda en el índice 2 y
   el store 2.x se appendea en el 7 → `import langfuse` = **4.15.2** (medido).
3. El store 2.x **no podría sostener este plugin ni siendo primero**: su `pydantic_core` es **cp313**
   (bajo 3.14 → `ModuleNotFoundError: pydantic_core._pydantic_core`) y bajo 3.12 importa 2.60.10 pero
   **sin `propagate_attributes` ni `LangfuseAPI`**, que es exactamente lo que importa la build.
4. La build instalada (v2.0.0) usa el SDK 4.x y **no tiene switch de transporte** (`grep -c TRANSPORT` → 0)
   → único transporte del SDK: **OTLP** a `{base}/api/public/otel/v1/traces`.
5. Servidor **2.95.11** (`GET /api/public/health` → 200 `OK`): `POST /api/public/otel/v1/traces` →
   **404** con HTML de Next.js (`__NEXT_DATA__`); `POST /api/public/ingestion` → **207**
   `{"successes":[],"errors":[]}`.
6. Logs del agente: **3.814** líneas `Failed to export span batch code: 404` en disco (88 archivos,
   **9 homes**), última **2026-11-03 18:28:02**. Por día: `10-27` 648, `11-02` 293, `11-03` 2.021.
   *La ventana rota*: una corrida previa (17:51) midió 5.313 líneas; el total en disco **no es monótono**
   porque los logs rotan — el conteo por día y el último timestamp son la lectura estable.
7. Servidor: **4.162** trazas (4.138 de flota + 24 sondas de este informe). Última de flota real
   **2026-10-14 (747)**; después solo canary: 4 el `11-02`, 7 el `11-03`.

Reproducible: `fix/precedence.py` → `fix/out/precedence.json`, `fix/facts.py` → `fix/out/facts.json`.

## 3. Matriz de variantes (proceso nuevo por caso, `PYTHONPATH` real)

| Caso | Config | Plugins | Callbacks | Dup | `transport_health()` | Entrega |
|---|---|---|---|---|---|---|
| M1 hoy | `enabled: [langfuse, observability/langfuse]`, dir `langfuse` = v2.0.0 | 1 | 11 | 0 | no existe el método | **NO** (OTLP 404) |
| M2 F5 in-place, otlp | `enabled: [langfuse-fleet]`, dir = F5 | 1 | 11 | 0 | `ok` | **NO** (404) — *health ok ≠ entrega* |
| M3 F5 in-place, legacy | ídem + `HERMES_LANGFUSE_TRANSPORT=legacy` | 1 | 11 | 0 | `ok` | **SÍ** (§7) |
| M4 dos dirs | `langfuse` (v2.0.0) + `langfuse-fleet` (F5), ambos habilitados | **2** | **22** | **11** | ok/ok | riesgo real de doble export |

**Matiz de `transport_health()` (medido):** antes del primer `_get_langfuse()` devuelve `disabled` con
razón vacía (es el default); recién después de construir el cliente reporta `ok`/`degraded`. Leerlo antes
del build produce un falso "disabled" en las 12 lecturas (`fix/out/delivery.json`:
`health_before_build.state=disabled`, `health.state=ok`).

## 4. Identidad del plugin: dos opciones, medidas

F5 publica `name: langfuse-fleet` y el gate compara contra `manifest_key`/`manifest.name`
(`hermes_cli/plugins_discovery.py:258-266`). Con F5 en su sitio, `enabled: [langfuse]` la deja como
*placeholder* ("not enabled in config"), **0 hooks**: la flota quedaría otra vez sin tracing, en silencio.

**Medido con el árbol F5 real (`fix/manifest_gate.py`):**

| Forma | Claves del registro | `enabled` | Hooks |
|---|---|---|---|
| F5 + `[langfuse]` | `langfuse-fleet` | false | **0** |
| F5 + `[observability/langfuse]` | `langfuse-fleet` | false | **0** |
| F5 + `[langfuse, observability/langfuse]` (8 perfiles hoy) | `langfuse-fleet` | false | **0** |
| F5 + `[langfuse-fleet]` | `langfuse-fleet` | **true** | **11** |
| v2.0.0 + `[langfuse]` (hoy) | `langfuse` | true | 11 |

**Opción A — renombrar el manifiesto F5 a `name: langfuse`.** Medida sobre una copia del árbol con solo
esa línea cambiada (`fix/option_a.py`): `[langfuse]`, `[observability/langfuse]` y ambas juntas cargan
**1 plugin / 11 hooks**. Cero cambios de config. Costo: `manifest.name` queda distinto del dir del repo
(`langfuse-fleet/`) y de la futura entrada de catálogo, y **no arregla la procedencia** (abajo).

**Opción B — migrar la config a `langfuse-fleet`.** Un renglón por home (§5), alineado con el nombre del
código, del dir y de la entrada futura de catálogo.

**Procedencia (decide el orden, no es cosmético).** El catálogo vivo tiene **una sola** entrada Langfuse:
`langfuse` v2.0.0 @ `e1f1614`. F5 **no está publicado**: en el clon local
(`repos/hermes-official-plugins`) el dir `langfuse-fleet/` está **sin trackear** (`git status` → `??`),
HEAD `de4c12a` en la rama `fix/langfuse-v2-rest-02-hermes-hooks`, y el `langfuse/` trackeado tiene el
transporte legacy pero su `__init__.py` (sha `944394f4`) **no** es el F5 (`81650ff6`). Los 11 dirs
instalados registran `catalog.name=langfuse` con pin `c2a9596` en `<home>/plugins/.install-metadata.json`.
Consecuencia medida en código (`hermes_cli/plugins_cmd_update.py`, `cmd_update`: rama
`if sidecar: cmd_update_catalog(...)`): si hay registro de catálogo,
`hermes plugins update langfuse` **re-pinnea a la entrada `langfuse` v2.0.0** → reinstala la build que
hoy no entrega. Sin registro y sin `.git`, el update **falla cerrado** ("Cannot update"), que es lo que
queremos mientras F5 viva solo en el árbol local.

**Recomendación: B, instalando F5 en su propio dir `plugins/langfuse-fleet` y borrando el dir viejo en
la misma ventana.** El exportador nuevo no es el viejo; reusar el nombre `langfuse` arrastra la
procedencia falsa del catálogo. A queda como plan de contingencia (medido, funciona) si se decide no
tocar config.

## 5. Cambio de configuración propuesto (clave final por home)

`plugins.enabled` final — se **quitan** `langfuse` y `observability/langfuse`, se **añade**
`langfuse-fleet`; el resto de las claves queda intacto:

| Home | `plugins.enabled` final (solo el tramo Langfuse) |
|---|---|
| root home | `…, inbox, `**`langfuse-fleet`**`, memory-wiki, …` (se elimina `langfuse`) |
| `bragi` | `homeassistant, `**`langfuse-fleet`**`, spotify, typesafe-skill-router` |
| `brokkr`, `comms`, `freyja`, `heimdall`, `roshi`, `sindri`, `ullr` | **`langfuse-fleet`**`, typesafe-skill-router` |
| `hermodr`, `vili` | **`langfuse-fleet`**`, typesafe-skill-router` |
| `profiles/default` | **decisión §9.3**: quitar `langfuse` (hoy habilita un plugin inexistente) o instalar el dir |
| `vigia` | sin cambios |

Además, por cada home desplegado:

1. **`HERMES_LANGFUSE_TRANSPORT=legacy`** en `<home>/.env` (ningún `.env` lo tiene hoy:
   `grep -l HERMES_LANGFUSE_TRANSPORT profiles/*/.env .env` → vacío). Sin esto, F5 vuelve a OTLP y a 404.
2. **Copiar el árbol F5 excluyendo** `spool.db`, `spool.db-shm`, `spool.db-wal`, `__pycache__/`,
   `.pytest_cache/`: el dir del repo trae un `spool.db` de pruebas que sembraría el spool de producción.
3. **Borrar la fila `langfuse`** de `<home>/plugins/.install-metadata.json` (y el dir viejo con su
   `.hermes-catalog.json`) para que `hermes plugins update` no pueda degradar a v2.0.0.
4. En `brokkr` y `heimdall` el dir `plugins/langfuse-fleet` ya existe **solo con `spool.db`** (canary).
   Contenido verificado: 122 done/7 lost en `brokkr` y 16 done/1 lost en `heimdall`, **todo en estado
   terminal** y con ids de prueba (`retry-budget-trace`, `split-trace`) → nada se re-entrega, pero se
   archiva como `spool.canary.db` para que la flota arranque con spool limpio.

No se toca: `hermes-agent` core, ni el servidor Langfuse, ni el gateway.

## 6. Orden de despliegue (y rollback en orden inverso)

**Fase 0 — congelar y respaldar (sin cambio de servicio).**
1. Prohibir `hermes plugins update|check-updates` sobre estos homes hasta publicar F5 (motivo §4).
2. Por home: `tar czf /var/backups/langfuse-<home>-20261103.tgz plugins/langfuse plugins/.install-metadata.json .env`
   (el backup vive **fuera** del árbol de plugins para no ser descubierto) y anotar el sha256 de
   `__init__.py`, `plugin.yaml` y el bloque `plugins.enabled`.
3. Anotar baseline: spool counts y último timestamp 404 por home (se comparan al final).

**Fase 1 — piloto `brokkr`** (tiene spool canary y es el perfil operador).
4. Archivar `plugins/langfuse-fleet/spool.db*` como `spool.canary.db`.
5. Copiar F5 → `plugins/langfuse-fleet/` (exclusiones del §5.2) y `chown -R hermes:hermes`.
6. Editar `plugins.enabled`: quitar `langfuse` y `observability/langfuse`, añadir `langfuse-fleet`.
7. Añadir `HERMES_LANGFUSE_TRANSPORT=legacy` al `.env` del home.
8. Reiniciar **solo el proceso de ese perfil** (no el gateway).
9. Verificar en proceso nuevo: 1 plugin, 11 hooks, `health.state=ok` tras el primer build; luego un turno
   real y readback por HTTP (traza + 2 observaciones) + spool `done`.
10. Recién entonces `rm -rf plugins/langfuse` y borrar su fila en `.install-metadata.json`.

**Fase 2 — resto, uno por home**: `root` → `heimdall` → `bragi` → `freyja` → `sindri` → `ullr` → `roshi`
→ `hermodr` → `vili` → `comms` (pasos 4-10 idénticos, verificación por home antes de seguir).

**Fase 3 — cierre**: `profiles/default` según §9.3 y re-medición global (§7).

**Rollback (orden inverso exacto, por home, sin git):**
10→4 invertido: restaurar `plugins.enabled` anterior → restaurar el dir `plugins/langfuse` desde el
tar (o `git`-less copy) → borrar `plugins/langfuse-fleet` (conservando `spool.canary.db` si existía) →
quitar `HERMES_LANGFUSE_TRANSPORT` del `.env` → reiniciar el proceso del perfil → verificar que vuelve a
`key=langfuse`, 11 hooks. Estado restaurado = "sin trazas", que es el estado actual: el rollback no
degrada nada que hoy funcione.

## 7. Verificación (dos bloques separados)

**A. Carga por home** — intérprete nuevo por home con la config propuesta
(`verify_final.py` → `out/verify.json`, corrido 18:2x, y `assert_verify.py` en verde):

| Métrica | Resultado |
|---|---|
| Homes con 1 plugin Langfuse | **12/12** |
| Hooks con callback | **11** en los 12 |
| Hooks duplicados | **0** |
| `transport_health().state` (tras build) | `ok` en los 12 |
| SDK resuelto | 4.15.2 |

**B. Entrega real por home** — `fix/delivery.py` → `fix/out/delivery.json`: un home desechable por
home con `enabled: [langfuse-fleet]` + `TRANSPORT=legacy`, un proceso nuevo cada uno, y **una traza real
por home** (trace id determinista, span + generation con `usage_details`). Resultado **12/12**:

| Comprobación | Resultado |
|---|---|
| Cliente construido | `LegacyIngestionClient`, `is_legacy_ingestion=True` (12/12) |
| Eventos en spool | `done: 5` por home, `pending_after_wait=0` (5 = trace + span + gen + 2 updates) |
| `GET /api/public/traces/<id>` crudo | **200** (12/12), `metadata.profile` y `turn_id` legibles |
| `GET /api/public/observations?traceId=` | **2** observaciones (span "Hermes turn" + generation) por home |
| `health_before_build` vs `health` | `disabled` → `ok` (semántica del §3) |

**C. Endpoint y spool del canary** (contexto, no sustituye a B): `POST /api/public/ingestion` → 207;
spool del canary real `profiles/brokkr/plugins/langfuse-fleet/spool.db` → **122 done / 7 lost**, y los 7
`lost` son las pruebas deliberadas de retry-budget del `2026-11-02` (`lost_types =
{retry-budget-trace: 7}`), no tráfico de la flota.

## 8. ¿Hay exportación duplicada hoy?

**No.** Un exportador por home y **cero exportaciones** desde `2026-10-14`. Readback HTTP crudo
(`fix/dup_readback.py` → `fix/out/dup_readback.json`, paginando los 4.162 registros y excluyendo las 24
sondas de este informe): **4.138** trazas de flota, **4.118** `turn_id`, y exactamente **2** repetidos —
`turn-f3-allowlist` y `f4-20261103T162030Z-…-0001`—, ambos con `profile=canary`, **trace ids distintos**
y 60,5 s / 76,5 s de separación → dos corridas del canary, no doble export. En las 12 trazas de flota más
recientes **ninguna** tiene dos observaciones raíz con el mismo nombre (la firma de dos exportadores
escribiendo el mismo turno): `duplicate_roots` vacío en todas.

El riesgo aparece **solo** en M4 (dos directorios, 22 callbacks, 11 hooks repetidos) o si se restaura la
entrega dejando la build vieja habilitada — exactamente lo que evita el paso 6 del §6.

## 9. Decisiones que necesito (Plon / Ragnar)

1. **Opción B (recomendada) u Opción A** para la identidad del plugin: B migra la clave a
   `langfuse-fleet`; A renombra el manifiesto y no toca config, pero deja `langfuse` sirviendo una build
   no publicada.
2. **Publicar F5** en el catálogo oficial (`langfuse-fleet`) o asumir el despliegue local con updates
   congelados: sin publicación, cada home queda con una build fuera de catálogo.
3. **Tracing en los 12 homes o solo en los operativos.** Hoy `hermodr` y `vili` entran solo por el alias
   legacy; `comms`, `vili` y `profiles/default` no tienen actividad en logs.
4. **`profiles/default`**: quitar la clave `langfuse` (mi recomendación) o instalar el plugin ahí.
5. **Servidor**: mantener 2.95.11 + transporte legacy (funciona hoy) o subir a v3 con OTLP nativo. Legacy
   deja al servidor sin ruta OTLP para cualquier otro cliente.

## 10. Evidencia en el workspace

| Script | Salida | Qué prueba |
|---|---|---|
| `fix/homes_inventory.py` | `fix/out/homes.json` | inventario por home (dir, build, catálogo, spool) |
| `fix/show_provenance.py` | stdout | registro del instalador + `.hermes-catalog.json` por dir |
| `fix/precedence.py` + `probe_import_langfuse.py` | `fix/out/precedence.json` | stores 2.x/4.x, inodos, `sys.path`, versión que gana |
| `fix/manifest_gate.py` | `fix/out/manifest_gate.json` | gate por forma de `plugins.enabled` con F5 |
| `fix/option_a.py` | `fix/out/option_a.json` | Opción A medida (manifiesto renombrado) |
| `fix/delivery.py` + `_deliver_runner.py` | `fix/out/delivery.json` | **entrega real por home** (spool + HTTP) |
| `fix/dup_readback.py` | `fix/out/dup_readback.json` | duplicación: 4.138 trazas, 2 dups de canary |
| `fix/facts.py` | `fix/out/facts.json` | health 2.95.11, OTLP 404, ingestion 207, 404 por día/home, spool canary |
| `verify_final.py` / `assert_verify.py` | `out/verify.json` | carga/hooks/health por home (12/12) |
| `fix/show_spools.py`, `fix/show_sidecars.py`, `fix/show_inventory*.py` | stdout | spools, sidecars, listas de claves |

Notas de honestidad: (a) las trazas de `fix/delivery.py` son **sondas marcadas** (`turn_id: deliv-*`) y
están identificadas en el servidor; (b) la traza `probe-0000` de la ronda anterior sigue en el servidor;
(c) el conteo de 404 en disco no es monótono por rotación de logs — usar el desglose por día.
