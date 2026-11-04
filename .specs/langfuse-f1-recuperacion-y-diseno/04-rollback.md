# 04 — Plan de Rollback

Principio: nada de este trabajo toca core Hermes, gateways, el plugin oficial instalado ni homes operativos; el rollback siempre es sustractivo y local al canario.

## R1 — Rollback de F1 (recuperación + documentos)

- Los documentos `.specs/langfuse-f1-recuperacion-y-diseno/*.md` son archivos nuevos sin trackear; borrar la carpeta los elimina por completo.
- El repo `/opt/hermes-official-plugins` queda exactamente en HEAD `de4c12af…` (worktree == HEAD verificado); restaurar «estado cero» = `rm -rf /opt/hermes-official-plugins` (el backup fuente `hermes-official-plugins-20261017.tar.gz`, SHA-256 `c380bc38…f0`, permanece inmutable como fuente de verdad).

## R2 — Rollback del spool (F3, futuro)

- El spool es un archivo sqlite por home (`<hermes_home>/plugins/langfuse-fleet/spool.db`): borrarlo elimina toda la persistencia sin efectos colaterales (no contiene contenido, solo metadatos allowlistados).
- Si el spool se corrompe: el cliente abre uno nuevo y arranca con contadores en cero; la pérdida se reconoce en el reporte (no se disimula: los contadores no se restauran desde memoria).

## R3 — Rollback del canario (F4, futuro)

- Desactivar el plugin: quitar `langfuse-fleet` de `plugins.enabled` del home canario (o `hermes plugins remove langfuse-fleet` en ese home). El plugin oficial `langfuse` se re-habilita SOLO en ese home si se desea volver al estado previo.
- No hay cambios en el server Langfuse PROD (solo ingesta vía API pública existente), por lo que no existe rollback de servidor.
- Si la observación de 24 h muestra pérdida inaceptable: se detiene la ingesta deshabilitando el plugin y el informe registra contadores finales; sin reintentos manuales ni migraciones.

## R4 — Si algo toca algo que no debía (defensa)

- Verificación post-rollback: `git -C /opt/hermes-official-plugins status --porcelain` = solo `.specs/` (o vacío tras R1); `git -C /opt/hermes-agent status` con sus 53 entradas dirty preexistentes intactas; gateways sin restart en todo el ciclo.
