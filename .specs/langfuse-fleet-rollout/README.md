# .specs/langfuse-fleet-rollout — RUNBOOK fuente del exportador único Langfuse

Estado: **documento/propuesta**. No autoriza despliegue, ni commit, ni aprobación de fase F4.
Arquitectura recomendada (validada por revisión independiente): **Opción B** (plugin separado
`langfuse-fleet` + clave migrada) con **`HERMES_LANGFUSE_TRANSPORT=legacy`** y **fuente pineada propia**.
Las **5 decisiones humanas** (identidad A/B, publicación en catálogo, alcance de homes, `profiles/default`,
servidor 2.95.11+legacy vs v3) siguen **abiertas** y no se resuelven aquí.

## Archivos (orden de lectura)

| Archivo | Qué es |
|---|---|
| `00-runbook-v2-backup.md` | **Backup inmutable** de la propuesta v2 (adjunto original t_d72cd140), byte a byte. No editar. |
| `10-runbook-v3.md` | **Runbook vigente**: correcciones C1–C5 sobre la v2. Fuente de verdad. |
| `90-diff-v2-v3.md` | Diff documental por ítem (qué decía la v2, qué dice la v3, dónde). |
| `20-evidencia-v3.json` | Snapshot saneado de la evidencia (sin secretos): hashes, conteos con alcance, spools, ids y timestamps. |
| `SHA256SUMS` | Manifiesto de integridad de los archivos de este directorio. |

## Reglas de este directorio

1. **Read-before-write:** antes de editar `10-runbook-v3.md`, leerlo completo y `90-diff-v2-v3.md`;
   todo cambio se anota en el diff y se re-genera `SHA256SUMS`.
2. `00-runbook-v2-backup.md` es **inmutable** (sha256 `f9dc6593…`). Si se requiere otra versión, se crea un
   archivo nuevo `11-runbook-v4.md`; nunca se sobrescribe el backup ni el adjunto original del tablero.
3. **Cero secretos** en este directorio y en sus adjuntos: los `.env` se citan por nombre de variable y estado,
   jamás por valor.
4. **Cero artefactos generados** aquí (spools, logs, snapshots): los snapshots van a
   `/var/backups/langfuse/…` (fuera del árbol de plugins); los temporales, a scratch, que no es fuente.
5. **Prohibido** convertir este runbook en excusa para tocar `hermes-agent`, el servidor Langfuse, gateways,
   perfiles o el canario F4 (`fleet-canary`, pid 691477).
6. El directorio está **sin trackear** (`?? .specs/`): cualquier commit requiere autorización explícita.

## Procedencia y límites

- Review que origina la v3: **t_5b23d653** (heimdall, run 136, `changes_required`, alcance documento).
- Repo objetivo: `/opt/hermes-agent-data/repos/hermes-official-plugins`, rama
  `fix/langfuse-v2-rest-02-hermes-hooks`, HEAD `de4c12af69e119e2fdde6d744fc8f19bb9e66cef`
  (verificado 2026-11-03 19:1x -05; **no** es `/opt/hermes-agent`).
- Limitación única declarada: la evidencia histórica del workspace `t_d72cd140` (`fix/`, `out/`) es
  irrecuperable; se sustituye por adjuntos durables con sha256 + snapshot RO nuevo (§10 del runbook).
