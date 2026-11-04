# 10 — Runbook v3: exportador único Langfuse por home (correcciones C1–C5)

Estado: **propuesta documental, NO aprobada para despliegue.** Corrige la v2
(`00-runbook-v2-backup.md`, sha256 `f9dc6593…`) según el veredicto independiente
`changes_required` de Heimdall (t_5b23d653, run 136). Arquitectura **Opción B + `HERMES_LANGFUSE_TRANSPORT=legacy`
+ fuente pineada propia** se mantiene como recomendación: la revisión independiente la validó y este runbook
no la re-decide. Las **cinco decisiones del §9 siguen abiertas y son HUMANAS** (Plon / Ragnar); nada aquí
las aprueba ni las resuelve implícitamente.

Autor: brokkr (t_5a4be53b) · Fecha de corrección: 2026-11-03 19:1x -05 (mediciones, ver `20-evidencia-v3.json`)
Alcance de este cambio: **documento**. Cero cambios de código, runtime, config, servicios, spool o credenciales.
F4 (`fleet-canary`) siguió corriendo aislado: no se detuvo, no se reinició, no se le generaron trazas.

---

## 0. Veredicto en una línea (sin cambios de fondo, con cifras re-medidas)

Hay **un exportador por home** (12 homes con la clave: 11 perfiles + home raíz; `profiles/default` habilita
`langfuse` sin tener el plugin → 0 exportadores en silencio) y **no entrega nada desde 2026-10-14**: emite OTLP
a un servidor **2.95.11** sin esa ruta (**404**, `text/html` de Next.js). La única variante que entrega es
**F5 + `HERMES_LANGFUSE_TRANSPORT=legacy`**, medida de punta a punta (24/24 trazas legibles, §7B/§7D).
A eso se suma un **13.º home** fuera de fases: el canario `fleet-canary` (§1.1), hoy el único F5 realmente
instalado y con el productor F4 vivo.

**Ventanas de medición (C5, corregido).** Son dos cosas distintas y la v2 las mezclaba:

| Ventana | Qué es | Rango real |
|---|---|---|
| Entrega de sondas §7B/§7D | trazas `deliv-*` selladas por el servidor | **18:17:07–18:18:03** y **18:18:41–18:19:30 (-05)** = `23:17:07.215Z–23:18:02.889Z` y `23:18:41.104Z–23:19:30.273Z` |
| Medición de carga/inventario §1/§3/§7A | procesos nuevos por home + inventario | **18:20–18:28 (-05)** = 23:20–23:28Z |

La carga se midió **después** de la entrega; la entrega **no** ocurrió dentro de la ventana de carga.
Son **24 ids `deliv-*` = 2 pasadas completas de 12 homes** (pasada 1 y pasada 2), no 12: §7B reporta una
pasada (12/12) y el servidor conserva las dos. Ver `20-evidencia-v3.json → c5_ventanas_y_origen`.

---

## 1. Inventario por home (12 con clave) + 13.º home fuera de fases (C4)

| Home | `plugins.enabled` (tramo Langfuse) | Dir del plugin | Build | Registro del instalador | Spool |
|---|---|---|---|---|---|
| **root home** `/opt/hermes-agent-data` | `langfuse`, `observability/langfuse` | `plugins/langfuse` | v2.0.0 | `catalog.name=langfuse`, pin `c2a9596` | — |
| `bragi` | `langfuse`, `observability/langfuse` | `plugins/langfuse` | v2.0.0 | ídem | — |
| `brokkr`, `comms`, `freyja`, `heimdall`, `roshi`, `sindri`, `ullr` | `langfuse`, `observability/langfuse` | `plugins/langfuse` (+ `plugins/langfuse-fleet` solo con `spool.db` en `brokkr`/`heimdall`) | v2.0.0 | ídem | canary: 122 done/7 lost y 16 done/1 lost |
| `hermodr`, `vili` | solo `observability/langfuse` (alias legacy) | `plugins/langfuse` | v2.0.0 | ídem | — |
| **perfil `default`** `profiles/default` | `langfuse` | **ninguno** | — | — | 0 exportadores, silencioso |
| `vigia` | sin claves Langfuse | — | — | — | — |

`observability/langfuse` **no** es un segundo plugin: `legacy_plugin_keys()`
(`hermes_cli/left_core_migration.py:229`) lo resuelve al mismo `langfuse`, y
`/opt/hermes-agent/plugins/observability/langfuse/` no tiene `plugin.yaml`. Hoy: 1 manifiesto, 11 hooks, 0 duplicados.

### 1.1 El 13.º home: `fleet-canary` — SEPARADO y FUERA de todas las fases (C4)

| Campo | Valor medido (2026-11-03 19:0x -05) |
|---|---|
| Ruta | `/opt/hermes-agent-data/fleet-canary` |
| `plugins.enabled` | `[langfuse-fleet]` |
| Árbol | F5 completo (`plugins/langfuse-fleet/`) |
| `.env` | `HERMES_LANGFUSE_TRANSPORT=legacy` presente |
| `.install-metadata.json` | **no existe** (instalación manual, sin registro de catálogo) |
| Productor F4 | `pid 691477`, `mtime` de arranque 18:32:25 -05, vivo (verificado por `ps`), 3 trazas `f4-*` en servidor 18:32:27 / 18:42:28 / 19:02:29 |
| Spool | `spool.db` 53.248 B (mtime 17:26:30) + `spool.db-wal` **341.992 B** (mtime 19:02:30) |

Reglas duras:
1. **No se cuenta entre los 12 desplegados**: es el 13.º y no aparece en `plugins.enabled` de ningún perfil.
2. **Fuera de Fase 0/1/2/3 y fuera del rollback**: no se copia, no se borra, no se re-sincroniza, no se reinicia.
3. **No se modifica su config, su `.env`, su árbol ni su spool** en ninguna fase. Es el productor de la
   observación F4 en curso; tocarlo invalida la ventana.
4. Su sincronización con el árbol del repo se **congela por sha** (`__init__.py 81650ff6…`, ver §10), no por
   copia automática. Cualquier re-sync futuro es una decisión humana aparte, con F4 detenida y spool preservado (§6).

---

## 2. Cadena causal (cifras re-medidas 2026-11-03)

1. `sys.path` real de los agentes: `PYTHONPATH=/opt/hermes-agent:<install-env>/venv/lib/python3.14/site-packages`.
2. Ese venv trae **langfuse 4.15.2** (inodo 5072069). El store **2.60.10 sí existe** en el inodo **2671944**
   (`/opt/data/lazy-packages` == `/opt/hermes-agent-data/lazy-packages`); lo decide el **orden de `sys.path`**
   (venv antes que el store). Bajo 3.12 el store importa 2.60.10 pero **sin `propagate_attributes`/`LangfuseAPI`**;
   bajo 3.14 ni siquiera importa (`pydantic_core` cp313). *Nota de reproducibilidad:* los índices numéricos de
   `sys.path` varían con el entorno (medidos 2/7 en un caso y 8/9 en otro); lo que importa y se reproduce es el orden.
3. La build instalada (v2.0.0, sha `d49fcf59…`) **no tiene switch de transporte** (`grep -c TRANSPORT` → 0):
   su único transporte es OTLP a `{base}/api/public/otel/v1/traces`.
4. Servidor **2.95.11**: `GET /api/public/health` → **200** `{"status":"OK","version":"2.95.11"}`;
   `POST /api/public/otel/v1/traces` → **404** `text/html` (Next.js). Control negativo re-medido hoy.
5. Logs del agente: **90 archivos** `Failed to export span batch code: 404` en **9 de los 14 homes/árboles
de log**, **5.122** ocurrencias, última **2026-11-03 19:12:45 -05** (§2.6, con el alcance declarado).
6. Servidor: **4.167** trazas totales. Ventana `2026-10-15 → 2026-11-01`: **0** trazas (re-medido hoy).
   Última de flota real **2026-10-14 (747)**; después solo canary/sondas.

### 2.6 Conteo de 404: alcance obligatorio (C3)

El número **no es citable sin declarar glob, raíz, profundidad, huso y hora de muestreo**. Medición de hoy:

- **Alcance A (el del runbook):** las **14 rutas** `<home>/logs/*.log` — `<home>` ∈ {`/opt/hermes-agent-data`,
los **12** directorios de `profiles/*/`, `fleet-canary`}, **profundidad 1**. En este host los árboles de
`logs/` son planos, así que hoy profundidad 1 == recursivo dentro de cada home.
Muestreo **2026-11-03 19:12:45 -0500 = 2026-11-04T00:12:45Z**. Resultado: **90 archivos, 9 homes con
coincidencias, 5.122 ocurrencias**. `fleet-canary` aporta 2 archivos y **0** ocurrencias (usa legacy:
justamente lo que se propone). Desglose completo en `20-evidencia-v3.json → c3_conteo_404`.
- **Alcance B (para demostrar que NO es comparable):** `find /opt/hermes-agent-data -path '*/logs*' -name '*.log'`
  (raíz distinta, recursivo, incluye `logs/curator/*`, `logs/process-results`, `logs/update_receipts`):
  **249 archivos, 5.147 ocurrencias**. Mismo día, misma palabra "logs", otro número.

**Retractación explícita.** Un borrador anterior citó 17.597 (alcance recursivo amplio) frente a 4.768/5.016
(profundidad 1) y lo interpretó como crecimiento anómalo ("5x"). Es diferencia de **alcance**, no de tasa:
queda retirado y **este runbook no repite esos números como comparables**. Lo estable es: **por home, el
último timestamp 404** y el conteo con alcance declarado. La conclusión —flota sin exportar, fallo vivo— no cambia.
No se adjuntan logs completos (contenido privado); se adjunta la **metadata de conteo** con su alcance.

---

## 3. Matriz de variantes (sin cambios; proceso nuevo por caso, `PYTHONPATH` real)

| Caso | Config | Plugins | Callbacks | Dup | `transport_health()` | Entrega |
|---|---|---|---|---|---|---|
| M1 hoy | `enabled: [langfuse, observability/langfuse]`, dir `langfuse` = v2.0.0 | 1 | 11 | 0 | no existe el método | **NO** (OTLP 404) |
| M2 F5 in-place, otlp | `enabled: [langfuse-fleet]`, dir = F5 | 1 | 11 | 0 | `ok` | **NO** (404) — *health ok ≠ entrega* |
| M3 F5 in-place, legacy | ídem + `HERMES_LANGFUSE_TRANSPORT=legacy` | 1 | 11 | 0 | `ok` | **SÍ** (§7) |
| M4 dos dirs | `langfuse` + `langfuse-fleet` ambos enabled | **2** | **22** | **11** | ok/ok | riesgo real de doble export |

`transport_health()` devuelve la última decisión de build del cliente, **no un GET**: leerlo antes del primer
`_get_langfuse()` da un falso `disabled` (§7). **`health ok` no es entrega**: en M2 da `ok` con 404 vivo.

---

## 4. Identidad del plugin (sin cambios): Opción B recomendada, A como contingencia

F5 publica `name: langfuse-fleet`; el gate compara contra `manifest_key`/`manifest.name`
(`hermes_cli/plugins_discovery.py:258-266`). Medido con el árbol F5 real: `[langfuse]`, `[observability/langfuse]`
y ambas juntas → **0 hooks**; solo `[langfuse-fleet]` → **11 hooks**. Opción A (renombrar el manifiesto) se midió
y funciona (1 plugin/11 hooks, sin tocar config) pero deja `langfuse` sirviendo una build no publicada.

**Procedencia (decide el orden).** El catálogo vivo tiene una sola entrada `langfuse` v2.0.0 @ `e1f1614`;
F5 **no está publicado** y en el clon local `langfuse-fleet/` está **sin trackear** (`git status` → `??`),
HEAD `de4c12af…`. Los 11 dirs instalados registran `catalog.name=langfuse` con pin `c2a9596`:
`hermes plugins update langfuse` **re-pinnea a v2.0.0** (la build que hoy no entrega). Sin registro ni `.git`,
el update falla cerrado, que es lo que se quiere mientras F5 viva solo en el árbol local.
**Consecuencia abierta:** antes de desplegar, el fuente de F5 debe versionarse con un commit **autorizado**
(hoy no lo está; la autorización previa fue solo `guard`, no commit de Langfuse).

---

## 5. Cambio de configuración propuesto (por home desplegado)

1. `plugins.enabled`: quitar `langfuse` y `observability/langfuse`, añadir **`langfuse-fleet`** (el resto intacto).
2. **`HERMES_LANGFUSE_TRANSPORT=legacy`** en el `.env` del home (hoy ausente en los 12; verificado).
3. Copiar el árbol F5 **excluyendo** `spool.db`, `spool.db-shm`, `spool.db-wal`, `__pycache__/`, `.pytest_cache/`
   (el `spool.db` del repo tiene **0 eventos**, pero se excluye igual: un spool de paquete nunca siembra producción).
4. Borrar la fila `langfuse` de `plugins/.install-metadata.json` **solo después** de §6.R1 (spool preservado).
5. En `brokkr`/`heimdall` el dir `plugins/langfuse-fleet` existe con spool canary terminal
   (122 done/7 lost y 16 done/1 lost, 0 pending): se archiva **con snapshot API + dump con IDs** (§6.R1),
   nunca con `cp` ni borrando el dir.
6. `fleet-canary` **no se toca** (§1.1).

No se toca: core de `hermes-agent`, servidor Langfuse, gateway, ni el canario F4.

---

## 6. Orden de despliegue y **rollback corregido** (C2)

### 6.1 Por qué la v2 estaba mal

`legacy_ingestion.py:107` → `default_spool_path() = $HERMES_HOME/plugins/langfuse-fleet/spool.db`.
**El spool vivo está DENTRO del directorio del plugin** que la Opción B instala. La v2 hacía
`borrar plugins/langfuse-fleet (conservando spool.canary.db)`: eso destruye eventos no entregados
(pending/inflight) y su estado. Medido hoy en el canario real, mismo archivo:

| Método de copia | Filas visibles | Qué se pierde |
|---|---|---|
| `cp spool.db` (solo el archivo, WAL fuera) | **49** | **15 eventos**, todos los del WAL (341.992 B) |
| `sqlite3 "file:…?mode=ro" ".backup snap.db"` (API de backup) | **64** | nada; `pragma integrity_check` → `ok` |

Los 64 son `done` (counters: `recovered=26`, `delivered=64`, `batches_acked=34`): en un home real con tráfico
esa diferencia sería backlog **no entregado**. Conclusión: **`cp` de `spool.db` está prohibido como snapshot**
y **borrar/re-copiar el dir del plugin antes de preservar el spool está prohibido**, punto.

### 6.2 Fase R0 — congelar, localizar el exportador, snapshot consistente

Regla: **solo se detiene el proceso dueño de ese home**, nunca el gateway ni otros perfiles, y solo con el
permiso específico que se autorice en el futuro. No se identifica un proceso por subcadena de `argv`
(regla HX003): el PID sale del registro/supervisor del perfil y se confirma leyendo `/proc/<pid>/environ`
en modo lectura para verificar que `HERMES_HOME` es el del home objetivo.

```bash
H=/opt/hermes-agent-data/profiles/<home>            # o /opt/hermes-agent-data
S=/var/backups/langfuse/<home>/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$S"

# (1) Detener SOLO el exportador de ese home (perfil dueño), con permiso específico.
#     Verificar antes: el proceso lee el spool de $H, no de otro home.

# (2) Snapshot CONSISTENTE por API de backup (no cp), con el proceso ya parado o quiescente:
sqlite3 "file:$H/plugins/langfuse-fleet/spool.db?mode=ro" ".backup '$S/spool.db'"

# (3) Integridad y conteo ANTES de borrar nada:
sqlite3 "$S/spool.db" "pragma integrity_check;"                       # exigido: ok
sqlite3 "$S/spool.db" "select state, count(*) from events group by state;"
sqlite3 "$S/spool.db" "select state, count(*) from events where owner is not null group by state;"

# (4) IDs recuperables sin depender del binario:
sqlite3 "$S/spool.db" ".dump" | gzip -9 > "$S/spool.dump.sql.gz"
sqlite3 "$S/spool.db" "select id, type, state from events order by state, id;" > "$S/spool.ids.tsv"

# (5) Hashes y manifiesto del snapshot:
sha256sum "$S/spool.db" "$S/spool.dump.sql.gz" > "$S/SHA256SUMS"
```

**Si el archivo necesita WAL/SHM** (proceso aún escribiendo y no se puede parar): usar la API de backup
(arriba), que ya incluye el contenido del WAL sin copiarlo a mano. Copiar el trío a mano
(`cp spool.db spool.db-wal spool.db-shm`) **solo** se admite con el exportador detenido y verificación
de `integrity_check` sobre la copia; nunca copias parciales mientras escribe.

**Criterio de aceptación R0 (bloqueante):**
- `integrity_check` = `ok` en el snapshot.
- `count(snapshot) >= count(live medido justo antes)`; ningún `id` de `state in ('pending','inflight','sending','lost')` falta del `dump`/`ids.tsv`.
- El snapshot y su dump quedan **fuera del árbol de plugins** (`/var/backups/langfuse/…`), nunca dentro de `$H/plugins`.
- Si no se cumple cualquiera de las tres: **STOP, no deploy, no rollback** (no se continúa "a ver si sale").

### 6.3 Fase R1 — preservar estado nuevo antes de tocar el dir

1. **Nunca borrar ni re-copiar `plugins/langfuse-fleet` antes de R0 completo.**
2. Si `pending > 0` (o `owner is not null` con estado no terminal): **no se borra nada**. Se preserva
   `spool.db` + dump + `ids.tsv` en `/var/backups/langfuse/<home>/<stamp>/`, se registra la lista de IDs y se
   migra explícitamente (re-encolar en el spool nuevo copiando las filas, o punto de reanudación por IDs).
   **Prohibido** arrancar el plugin con un spool vacío dejando atrás eventos pendientes.
3. Retención: se conservan **los dos últimos snapshots por home**; **nunca se borra el último**.
4. `pending=0` **no** se declara con productores activos ni con `inflight>0`. La frase válida es:
   `pending=0, inflight=0, productores detenidos/observados en ventana de <t1>–<t2>`.
   Un `pending=0` medido mientras `owner` tiene filas no terminales es un reporte engañoso y se rechaza.

### 6.4 Rollback (orden inverso exacto, por home, sin git) — corregido

```
# 0. R0/R1 ejecutados: snapshot consistente + dump + ids + pending tratado.
# 1. Restaurar plugins.enabled anterior (langfuse + observability/langfuse).
# 2. Restaurar el dir plugins/langfuse desde su tar de Fase 0 (vive fuera del árbol de plugins).
# 3. ANTES de borrar el dir nuevo: confirmar que su spool está preservado (R0) y su conteo coincide.
# 4. Borrar plugins/langfuse-fleet SOLO con el snapshot verificado; si hubo eventos nuevos desde R0,
#    repetir R0 (nuevo stamp) y no borrar el anterior.
# 5. Quitar HERMES_LANGFUSE_TRANSPORT del .env del home.
# 6. Reiniciar solo el proceso del perfil; verificar key=langfuse, 11 hooks, health tras build.
# 7. Reversibilidad: el snapshot restaurable debe seguir leyéndose (integrity ok) y con el mismo conteo.
```

**Estado restaurado esperado:** "sin trazas", que es el estado actual — el rollback no degrada nada que hoy
funcione, **pero sí puede destruir backlog** si se salta §6.2/§6.3. Esa es la corrección de C2.

Pruebas del rollback (documentales, ejecutables sin tocar producción): sobre copias en `/opt/.../cache/scratch/`
de un spool con `pending>0` sembrado, ejecutar R0+R1 y verificar por comando (a) `integrity_check=ok`,
(b) `count(ids)` antes == después, (c) `pending` tras migración == 0 con `inflight=0`. **No se ejecuta hoy
sobre el spool real** (§9).

---

## 7. Verificación (dos bloques separados, más un tercero de control)

**A. Carga por home** — intérprete nuevo por home con la config propuesta (`proposed_enabled=[langfuse-fleet]`
en `verify.json`; el caso fuerte —lista real de `brokkr` y `bragi` menos las dos claves Langfuse más
`langfuse-fleet`— se cerró aparte): **12/12** homes con 1 plugin, **11 hooks**, **0 duplicados**,
`transport_health=ok` tras el build, SDK 4.15.2.

**B. Entrega real por home** — home **desechable** por home (NO los 12 perfiles operativos), proceso nuevo,
`enabled: [langfuse-fleet]` + `TRANSPORT=legacy`, una traza `deliv-<home>` por home: **12/12** con
`LegacyIngestionClient`, `done: 5` en spool, `pending_after_wait=0`, `GET /api/public/traces/<id>` **200** y
**2 observaciones** (span + generation). Re-medido hoy contra el servidor vivo: **24 ids** (dos pasadas),
timestamps en §0 y en `20-evidencia-v3.json`. **Esto prueba el transporte en homes de prueba, no que los 12
perfiles operativos entreguen**: eso solo se demuestra después de desplegar y con un turno real por home.

**C. Control negativo (obligatorio junto a B)** — mismo credencial alcance, transporte por defecto:
`POST /api/public/otel/v1/traces` → **404 text/html** (re-medido hoy, sin credenciales), mientras el
transporte legacy entrega por REST. Si alguien reporta "entrega ok" sin este par, el reporte no vale:
`health ok` y `transport_health ok` no son entrega (§3).

---

## 8. ¿Hay exportación duplicada hoy? (sin cambios)

**No.** Un exportador por home y cero exportaciones de flota desde 2026-10-14. Readback paginado excluyendo
sondas: ~4.118 `turn_id`, 2 repetidos **con trace ids distintos** y ~60–76 s de separación → dos corridas del
canario, no doble export. El riesgo aparece solo en M4 (dos dirs, 22 callbacks, 11 hooks repetidos) o si se
restaura la entrega dejando la build vieja habilitada — exactamente lo que evita §5.1.

---

## 9. Decisiones que necesito (Plon / Ragnar) — 5, ABIERTAS, no aprobadas

1. **Opción B (recomendada) u Opción A** para la identidad del plugin.
2. **Publicar F5** en el catálogo o asumir despliegue local con updates congelados.
3. **Tracing en los 12 homes o solo en los operativos.**
4. **`profiles/default`**: quitar la clave (recomendado) o instalar el plugin.
5. **Servidor**: mantener 2.95.11 + legacy o subir a v3 con OTLP nativo.

Siguen sin resolver. Ninguna se resuelve implícitamente por este runbook ni por la revisión de documento.
**Riesgos declarados (no secretos):** `health ok` no es entrega; `hermes plugins update` del catálogo
sobrescribe/re-pinnea F5 a `c2a9596` mientras no se publique o congele; el fuente de F5 está sin trackear y
requiere un commit autorizado antes de desplegar; el último tráfico real de flota es **2026-10-14**.

---

## 10. Evidencia: histórico irrecuperable + snapshot nuevo (C1)

### 10.1 Lo que NO se puede recuperar (declarado, no fingido)

`/opt/hermes-agent-data/kanban/workspaces/t_d72cd140/` **ya no existe** (verificado 2026-11-03 19:1x -05:
solo quedan 6 workspaces de otras tarjetas). Por tanto:

- Los 10 scripts `fix/*.py` y las 10 salidas `fix/out/*.json` citados en la §10 de la v2 son **ilegibles hoy**.
- `review_r2_evidence.json.reviewer_evidence` apunta a `/…/t_d72cd140/r2rev/`, también inexistente.
- **No se genera un `tar` de esas rutas**: fingir un archivo de un directorio borrado (o "reconstruir" salidas)
  es exactamente lo que este runbook prohíbe. La ausencia se declara, no se rellena.

Lo que **sí** sobrevive, como adjuntos durables del tablero (no en workspaces ni scratch):

| Adjunto | sha256 | Creado (UTC) |
|---|---|---|
| `t_d72cd140-propuesta-exportador-unico.md` (v1) | `8acca4421e05044242dcb0457fa01f8232e8eb552b9c787d6982a9ec00a11e75` | 2026-11-03T22:52:08Z |
| `t_d72cd140-propuesta-exportador-unico.v2.md` (v2) | `f9dc65932d74dbcbf7ddd1776e170859731a36c66cdb761c8516b8fec706951a` | 2026-11-03T23:37:31Z |
| `review_r2_evidence.json` (r2) | `f4526195b85d97069af882ed27defba6bd65a3afb6fdddf66f18ee76d744d41f` | 2026-11-03T23:56:08Z |
| `review_t_5b23d653_evidence.json.gz` (R2 independiente) | `2309ef47c785b435af188bef140f4a9e4c9bdec2c80aec06d70130ffc7c74e0a` | 2026-11-04T00:08:22Z |

La v2 queda **copiada byte a byte** en `00-runbook-v2-backup.md` (mismo sha256 `f9dc6593…`); el adjunto
original **no se sobrescribe**.

### 10.2 Snapshot NUEVO, solo lectura (lo que sí es reproducible hoy)

Todo lo de abajo se midió hoy contra el sistema vivo, **sin generar eventos, sin POST, sin hooks exportadores,
sin credenciales en stdout y sin cambios de config**. Comandos y resultados completos en
`20-evidencia-v3.json`:

| Hecho | Valor | Cómo |
|---|---|---|
| Repo fuente | `de4c12af69e119e2fdde6d744fc8f19bb9e66cef`, rama `fix/langfuse-v2-rest-02-hermes-hooks`; `git status` solo `?? .specs/`, `?? langfuse-fleet/` | `git rev-parse HEAD`, `git status --porcelain` |
| F5 `__init__.py` | `81650ff69d4d87816a41090b…` | `sha256sum` |
| F5 `legacy_ingestion.py` | `f24edd55ab88d507115c3311…` | `sha256sum` |
| F5 `plugin.yaml` / `fleet_spool.py` / `sdk_env.py` | `c20b09e9…` / `9ebab73b…` / `ecbb0645…` | `sha256sum` |
| `langfuse/` trackeado (legacy, no F5) | `__init__.py 944394f4…`, `plugin.yaml 00998e10…` | `sha256sum` |
| Spool del repo (excluido de copia) | 0 eventos | `sqlite3 …mode=ro` |
| Spool canary `fleet-canary` | backup API: 64 rows `done`, `integrity_check=ok`, counters 26/64/34 | `.backup` + queries (§6.1) |
| Spool canary `brokkr` / `heimdall` | 122 done/7 lost · 16 done/1 lost | `sqlite3 mode=ro` |
| Conteo 404 | §2.6 (alcance A y B) | `grep -c` por home |
| Trazas `deliv-*` | 24 ids, 2 pasadas, timestamps UTC exactos | `GET /api/public/traces?fromTimestamp=…` |
| Flota sin exportar | 0 trazas en `2026-10-15 → 2026-11-01`; total 4.167 | `GET …/traces` (`meta.totalItems`) |
| Control negativo | `health=200`, `otlp=404 text/html` | `GET /api/public/health`, `POST /api/public/otel/v1/traces` |
| F4 vivo y no tocado | `pid 691477`, arranque 18:32:25 -05, 3 trazas `f4-*` | `ps`, `GET …/traces` |
| Canario 13.º | `enabled=[langfuse-fleet]`, `TRANSPORT` presente, sin `.install-metadata.json` | lectura de `config.yaml`, `.env` (solo nombres/estado), `ls` |

**Lo que queda como limitación única y declarada:** la evidencia histórica de la §10 de la v2
(scripts y salidas de `fix/out/`) es **irrecuperable**; en su lugar hay (a) los adjuntos durables con sha256 de
§10.1, y (b) el snapshot nuevo RO de §10.2, reproducible con los comandos de `20-evidencia-v3.json`.
No se promete recuperación de lo que no existe.

**Pruebas de código/store/import** (si se re-ejecutan): en proceso PROPIO con metadatos (sin inicializar hooks
exportadores ni el SDK con credenciales) y sin cambios de config; nada de helpers ad hoc fuera de git —
si hace falta un script nuevo, va al repo con su propio PR/commit autorizado.

---

## 11. Cierre

- Este runbook es **documento**: no autoriza deploy, no aprueba F4, no autoriza commit/push/PR/merge de Langfuse,
  no toca credenciales/perfiles/gateways.
- Ciclo cerrado en `kanban_request_review(reviewer='heimdall')`: la revalidación es **solo documental**
  (límites y rollback). No requiere otra ronda de arquitectura, ni POST nuevos, ni tocar F4.
- Cambios C1–C5 y su ubicación exacta: `90-diff-v2-v3.md`.
