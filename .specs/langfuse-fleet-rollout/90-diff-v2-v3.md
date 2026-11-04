# 90 — Diff documental v2 → v3 (C1–C5)

Base: `00-runbook-v2-backup.md` (sha256 `f9dc65932d74dbcbf7ddd1776e170859731a36c66cdb761c8516b8fec706951a`,
copia byte a byte del adjunto original de t_d72cd140; el original no se toca).
Resultado: `10-runbook-v3.md`.
Origen del cambio: review independiente **t_5b23d653** (heimdall, run 136, `changes_required`, alcance documento).

Sin cambios de arquitectura: Opción B + `HERMES_LANGFUSE_TRANSPORT=legacy` + fuente pineada propia siguen
siendo la recomendación validada. Las 5 decisiones humanas siguen abiertas.

---

## Resumen por ítem

| Ítem | Severidad | Qué decía la v2 | Qué dice la v3 | Sección v3 |
|---|---|---|---|---|
| **C1** | alta (trazabilidad) | §10 citaba 10 scripts y 10 salidas de `kanban/workspaces/t_d72cd140/fix/…` como evidencia reproducible | Declara la **irrecuperabilidad** del workspace (ya no existe), prohíbe fabricar `tar`/reconstruir salidas, conserva los adjuntos originales con sha256 y aporta un **snapshot nuevo solo lectura** (repo HEAD, hashes de F5, spools, conteo 404 con alcance, 24 ids `deliv-*`, control negativo) con comandos | §10.1, §10.2 |
| **C2** | alta (pérdida de datos) | Rollback: "borrar `plugins/langfuse-fleet` (conservando `spool.canary.db`)" | El spool vive **dentro** de ese dir (`legacy_ingestion.py:107`). Snapshot **consistente por API de backup**, `integrity_check`, conteo antes/después, preservación de `pending>0` con dump+IDs y migración explícita, prohibición de `cp` de `spool.db`, retención de 2 snapshots, **STOP/no-deploy** si no se puede preservar, prohibido `pending=0` con `inflight>0` | §6.1–§6.4 |
| **C3** | baja (métrica) | §2.6 citaba "3.814 líneas / 88 archivos" y en otra ronda 17.597 vs 4.768 como crecimiento | Números **solo con alcance declarado** (glob, raíz, profundidad, TZ, hora de muestreo): 90 archivos / 9 de 14 árboles / 5.122 a las 19:12:45 -05; alcance B (249 archivos / 5.147) mostrado como **no comparable**; retractación explícita del "5x" | §2.6, §2.5 |
| **C4** | media (alcance) | 12 homes; el canario no aparecía | Nuevo §1.1: **13.º home `fleet-canary`** (enabled `[langfuse-fleet]`, TRANSPORT legacy, sin install-metadata, F4 pid 691477 vivo) declarado **fuera de fases, copias y rollback**; no se cuenta entre los 12 | §1.1, §5.6, §6.4 |
| **C5** | baja | "medido entre 18:20 y 18:28" con 12 ids | Ventanas separadas: entrega `deliv-*` **18:17:07–18:18:03** y **18:18:41–18:19:30 -05** (24 ids = 2 pasadas de 12) vs medición de carga **18:20–18:28**; timestamps UTC por artefacto y por traza; aclara que la entrega fue en **homes desechables**, no en los 12 perfiles operativos; control negativo OTLP 404 vs REST legacy | §0, §7B, §7C, §10.2 |

---

## Detalle C1 — evidencia (§10)

- **Antes:** tabla de 10 scripts (`fix/homes_inventory.py`, `fix/precedence.py`, `fix/delivery.py`, …) y 10 salidas
  (`fix/out/*.json`) como prueba reproducible, más `reviewer_evidence: …/t_d72cd140/r2rev/`.
- **Hecho verificado:** esos directorios **no existen** (`/opt/hermes-agent-data/kanban/workspaces/` solo tiene 6
  workspaces de otras tarjetas). Un `tar` de rutas inexistentes sería ficción documental.
- **Después:**
  - §10.1 declara la ausencia y lista los 4 adjuntos durables con sha256 + hora UTC de creación.
  - §10.2 aporta el snapshot RO nuevo con comandos y resultados reales de hoy (HEAD, sha256 de F5, spools,
    404 con alcance, 24 ids con timestamps, `health=200`, `otlp=404`, ventana 15-oct→01-nov = 0 trazas).
  - Los 24 ids `deliv-*` **son reidentificables desde el servidor** (`fromTimestamp`), así que la entrega queda
    auditable aunque `delivery.json` se haya perdido.
- **Límite declarado:** el histórico de `fix/out/` es irrecuperable. No se promete recuperación.

## Detalle C2 — rollback (§6)

- **Antes:** paso 10 invertido: restaurar config → restaurar dir → **borrar `plugins/langfuse-fleet`
  (conservando `spool.canary.db`)** → quitar TRANSPORT → reiniciar.
- **Riesgo real medido:** `default_spool_path()` = `$HERMES_HOME/plugins/langfuse-fleet/spool.db`, dentro del dir
  que se borraba. En el canario vivo, `cp` de solo `spool.db` ve **49** filas; la API de backup ve **64**
  (`integrity_check=ok`): **15 eventos** vivían solo en el WAL de 341.992 B. En un home con tráfico eso es backlog
  no entregado destruido por el rollback.
- **Después:** R0 (congelar, localizar el PID por registro del perfil + `/proc/<pid>/environ`, snapshot por
  `.backup`, `integrity_check`, conteo de estados, dump + `ids.tsv`, sha256, todo fuera del árbol de plugins),
  R1 (no borrar ni re-copiar el dir, preservar `pending>0`, retención 2, prohibido `pending=0` engañoso),
  y rollback 7 pasos con criterio de aceptación y condición STOP.
- **No ejecutado:** nada de R0/R1/rollback se corrió sobre el spool real ni sobre perfiles reales.

## Detalle C3 — conteo de 404 (§2.6)

- **Antes:** cifras sin glob ni hora; en la ronda previa se llegó a afirmar crecimiento ~5x comparando 17.597
  (recursivo amplio) con 4.768/5.016 (profundidad 1).
- **Después:** dos alcances declarados y medidos hoy, con TZ y hora. La diferencia se atribuye a **alcance**,
  no a tasa; el "5x" queda retirado por escrito. Lo estable es el último timestamp por home.
- **Adjuntos:** solo metadata de conteo (`20-evidencia-v3.json → c3_conteo_404`), nunca logs completos.

## Detalle C4 — 13.º home (§1.1)

- **Antes:** "12 homes" sin mención del canario; el lector podía creer que F5 no corría en ningún lado o que
  12/12 cubría toda la flota.
- **Después:** §1.1 con la tabla del canario y 4 reglas duras (no cuenta entre 12, fuera de fases, no se toca,
  sincronización congelada por sha). §5.6 y §6.4 lo excluyen explícitamente de copias y rollback.

## Detalle C5 — ventanas (§0 y §7)

- **Antes:** una sola frase "medido entre 18:20 y 18:28", con 12 ids.
- **Después:** tabla de dos ventanas con hora local y UTC, las dos pasadas completas (12+12 = 24 ids con
  timestamps UTC por traza en la evidencia), y la aclaración de que la entrega se midió en **homes desechables**,
  no en los 12 perfiles operativos. Ningún timestamp se ajustó para cuadrar el relato: los de entrega salen del
  servidor, no del documento.

---

## Verificación del cambio (documental)

```bash
cd /opt/hermes-agent-data/repos/hermes-official-plugins
sha256sum .specs/langfuse-fleet-rollout/00-runbook-v2-backup.md   # = f9dc6593… (adjunto original)
sha256sum .specs/langfuse-fleet-rollout/*.md .specs/langfuse-fleet-rollout/*.json
git status --porcelain      # solo ?? .specs/  ?? langfuse-fleet/  (0 archivos trackeados tocados)
```

Criterio de aprobación del reviewer (solo documental): C1..C5 cerrados en §10/§6/§2.6/§1.1/§0; arquitectura
intacta; 5 decisiones abiertas intactas; sin deploy, sin commit, sin POST nuevos, sin tocar F4.
