# F1 — Informe de Integridad y Recuperación del Repositorio

Fecha: 2026-11-02 16:30–17:00 (-05). Ejecutante: brokkr (t_86362f3f). Host: DEV (local).

## 1. Fuente y destino

| Campo | Valor |
|---|---|
| Backup fuente (INMUTABLE, sin modificar) | `/opt/hermes-agent-data/backups/hermes-official-plugins-20261017.tar.gz` |
| Tamaño fuente | 15,862,048 bytes |
| SHA-256 del tar | `c380bc385263c50b64c8b82539dde4cee23a0835fdb0bde791f000dd04fde9f0` |
| Destino | `/opt/hermes-official-plugins` (ausente antes de la extracción; verificado dos veces: pre-extracción y pre-`mkdir`) |
| Extracción | `tar -xzf … -C /opt` como usuario `hermes`; directorio base creado por root vía `ssh dev` con `chown hermes:hermes` puntual (único escalado de permisos; sin chown/chmod recursivo) |

## 2. Validación previa del tar

- 394 entradas: 108 directorios, 286 archivos regulares. **0 symlinks, 0 hardlinks, 0 dispositivos, 0 FIFOs.**
- Sin rutas absolutas ni componentes `..` (grep sobre `tar -tzf` antes de extraer).
- Raíz única `hermes-official-plugins/`. Contenido: los plugins oficiales del catálogo (`langfuse/`, `homeassistant/`, `spotify/`, `claude-subscription-directsdk/`, etc.) más `.git/` completo.
- No se ejecutó ningún hook de Git; no se leyó ni imprimió ningún secreto (`.git/config` contiene solo la URL pública `https://github.com/NousResearch/hermes-official-plugins.git`).

## 3. Verificación Git del repo recuperado

- `git fsck --full`: **sin errores**. Solo 3 dangling trees (restos normales de reflog/index; no afectan la historia).
- HEAD = `de4c12af69e119e2fdde6d744fc8f19bb9e66cef` (unit 2 «feat(langfuse): wire opt-in legacy transport into Hermes hooks», 2026-10-16 23:34:49 -0500).
- Parentado exacto verificado con `git log --format='%H %P %s'`:
  - `c2a95963c442f3f23e4bc6fe0da898e1dd1fb63d` (base oficial, rama `fix/langfuse-v2-rest`)
  - → `e52545c16942f33398c371a75a0cea5ffdacec4f` (unit 1, rama `fix/langfuse-v2-rest-01-transport`, 2026-10-16 22:59:45 -0500)
  - → `de4c12af69e119e2fdde6d744fc8f19bb9e66cef` (unit 2, rama `fix/langfuse-v2-rest-02-hermes-hooks`)
- Los 3 objetos existen y son tipo `commit` (`git cat-file -t`); `c2a95963` está contenido en `main` (base oficial remota `origin/main` = `fe39e8f`).
- `git status --porcelain`: **0 entradas** — worktree idéntico a HEAD (`git diff HEAD` vacío; `git diff HEAD -- langfuse` vacío).
- 172 archivos trackeados. Ramas presentes: `main`, `fix/langfuse-v2-rest`, `fix/langfuse-v2-rest-01-transport`, `fix/langfuse-v2-rest-02-hermes-hooks`.

## 4. Reglas propias del repo (pre-flight SDD)

- El repo recuperado **NO tiene** `AGENTS.md`, `PROJECT_MEMORY.md` ni `.agents/` en la raíz (ausencia registrada, no inventada). Único AGENTS.md dentro del árbol: `claude-subscription-directsdk/AGENTS.md`, ajeno a este alcance.
- Se aplicaron las reglas del repo host `/opt/hermes-agent`: `AGENTS.md` (en contexto), `.agents/rules/01-sdd-workflow.md` (ciclo SDD de 4 fases), `plugins/AGENTS.md` (plugins nunca tocan core, catálogo único, contrato de compatibilidad, secret_scope sin fallback a `os.environ`), y `PROJECT_MEMORY.md` § «Lightweight Hermes Observability on Existing Langfuse (2026-10-17)» como contexto histórico.
- Nota: el skill `hermes-agent` del perfil está **en cuarentena** por el escáner de seguridad y no cargó; se trabajó directamente contra los documentos del repositorio, que es la fuente canónica equivalente.

## 5. Re-ejecución de pruebas (la evidencia histórica ya no cuenta)

| Suite | Comando | Resultado en vivo |
|---|---|---|
| Suite completa del plugin | `cd /opt/hermes-official-plugins/langfuse && PYTHONPATH=/opt/hermes-agent /usr/bin/python3.12 -m pytest -q` | **155 passed, 2 skipped** (idéntico al histórico) |
| Contrato SDK standalone | `cd /opt/hermes-official-plugins/langfuse && python3 tests/test_legacy_sdk_contract.py` (venv PM 3.14 con SDK pin) | **exit 0** — «SDK4 serializer, retry, split-batch, and blocked-transport contracts passed» |

Interpretes: pytest 9.1.1 en `/usr/bin/python3.12`; SDK `langfuse==4.15.2` verificado en el venv PM (`langfuse-4.15.2.dist-info`). El módulo `test_scoped_secret_reader.py` requiere el core Hermes en `PYTHONPATH` (import `agent.secret_scope`); es la forma histórica de ejecución y se documenta aquí.

## 6. Estado y frontera de escrituras

- Escrituras realizadas: solo la extracción a `/opt/hermes-official-plugins` y esta carpeta `.specs/` de documentos SDD nuevos (sin commit: **no hay autorización de pull/commit/push**).
- Backup fuente intacto (hash verificado antes de usar; no se abrió en modo escritura).
- `/opt/hermes-agent` (workspace del host): sin modificar; sus 53 entradas dirty preexistentes no fueron tocadas.
