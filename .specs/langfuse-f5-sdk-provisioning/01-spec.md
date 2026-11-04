# 01 — Especificación (F5: provisión del SDK Langfuse 4 para el plugin standalone)

Estado: aprobada por el hallazgo de t_aed35658 (fallo silencioso verificado en turno real).
Alcance: `langfuse-fleet` únicamente. No toca el plugin core `observability/langfuse` ni el store compartido `langfuse 2.x`.

## 1. Intención

El plugin `langfuse-fleet` declara `langfuse==4.15.2` en `pyproject.toml`, pero Hermes no instala dependencias Python de plugins de usuario/standalone. En un turno real (`HERMES_HOME=/opt/hermes-agent-data/fleet-canary`) el plugin registró sus 11 hooks, el turno terminó "exitoso" (2 llamadas API), y **no se exportó nada**: spool sin filas nuevas, 0 trazas en Langfuse. La única señal fue un WARNING genérico (`Could not initialize Langfuse legacy REST client`) sin causa ni remedio.

### 1.1 Contrato de dependencias (confirmado en el código de Hermes, no asumido)

Atribuir el fallo genéricamente a "Hermes no instala dependencias" era correcto a medias; el contrato real es:

| Superficie | Comportamiento | Evidencia |
|---|---|---|
| Descubrimiento (`hermes_cli/plugins_loader.py::_warn_python_dependencies`) | Las deps del manifest se **validan y reportan**, nunca se instalan. El aviso sugiere `hermes pm repair` y comprueba `importlib.metadata` **del entorno del proceso**, no de un store propio. | `plugins_loader.py:186-199, 412-430` |
| PM (`pm/plugin_declarations.py`) | Sólo construye en el venv compartido los plugins **miembros** (`is_member = not external and (pyproject or requirements)`). `python_runtime: external` los saca del build: deps ignoradas. | `plugin_declarations.py:33-40, 120-151` |
| Runtime del plugin | Nada instala el SDK por él: si el import falla, los hooks quedan inertes (fail-open) y el turno "pasa". | `__init__.py::_build_client`, `legacy_ingestion.py::create_langfuse_api_client` |

Consecuencia de diseño: **no** se puede volver miembro. El venv compartido es único y en este host 10 perfiles tienen habilitado el plugin core `observability/langfuse`, que usa la API **2.x** desde `/opt/data/lazy-packages`; meter `langfuse 4.15.2` en el venv cambiaría el SDK bajo ese plugin en todos esos perfiles. La forma soportada por el propio contrato de PM es el sidecar: `python_runtime: external` + store propio pineado por intérprete, resuelto por el plugin (`sdk_env.py`) y provisionado por un script de despliegue.

Nota: `python_dependencies` **no** se declara en `plugin.yaml`. Su único efecto aquí sería un aviso de descubrimiento "declares Python dependencies that are not installed / run hermes pm repair" — falso para un sidecar que PM no debe construir. El pin vive en `pyproject.toml` (superficie de empaquetado), `sdk_env.SDK_VERSION` y el script.

## 2. Causa raíz (verificada)

1. `legacy_ingestion.create_langfuse_api_client` hace `from langfuse.api.client import LangfuseAPI` en tiempo de entrega.
2. `/opt/hermes-agent/.venv` (Python 3.14.7) no tiene `langfuse` → `ModuleNotFoundError`.
3. El store lazy compartido `/opt/hermes-agent-data/lazy-packages` sólo tiene `langfuse 2.60.10`, cuya superficie de API es distinta: `from langfuse.api.client import LangfuseAPI` → `ImportError: cannot import name 'LangfuseAPI'`. No sirve para este plugin y no debe modificarse (lo usa el plugin oficial con API 2.x).
4. El plugin core resuelve su store con un bootstrap de `sys.path`; la variante standalone `langfuse-fleet` no tenía bootstrap ni store propio.

## 3. Criterios de aceptación

- **AC1 Store por intérprete:** Given el plugin importado, Then resuelve candidatos de store en este orden: `HERMES_LANGFUSE_SDK_PATH` (override explícito, separable por `os.pathsep`) y `<base>/py<major><minor>`; los directorios existentes se **añaden al final** de `sys.path`. El store es un árbol `--target` completo (trae su propio `pydantic`/`httpx`): lo contractual es el orden, de modo que donde el intérprete ya provee esos paquetes (el venv de Hermes) su copia conserva precedencia, y donde no los provee el store los aporta.
- **AC2 Aislamiento:** Given el store compartido `/opt/data/lazy-packages` (langfuse 2.x), Then el bootstrap **nunca** lo añade por su cuenta.
- **AC3 Fallo visible:** Given el SDK ausente/ilegible, When se construye el cliente por primera vez — **en cualquiera de los dos transportes, incluido `otlp` (el default)** —, Then se emite **un solo** WARNING accionable (excepción concreta, stores buscados, intérprete, comando de provisión) — no un mensaje genérico ni un remedio inejecutable para un sidecar — y no se repite por cada hook/turno.
- **AC4 Estado consultable:** Given el cliente construido o degradado, Then `transport_health()` devuelve `{state: "ok"|"degraded"|"disabled", reason, python, stores}` para diagnóstico sin leer logs.
- **AC5 Sin regresión:** Given SDK 4 disponible, Then el flujo legacy (spool → worker → REST v2) no cambia y la suite `langfuse-fleet/tests` sigue en verde.
- **AC6 Fin a fin:** Given un turno canario real sin `PYTHONPATH` overlay, Then las filas del spool pasan a `done` y la traza aparece en Langfuse con jerarquía turno→generaciones→tool.

## 4. Fuera de alcance

- Instalar dependencias en el venv de Hermes (prohibido: contamina todos los perfiles).
- Modificar/actualizar el store compartido `langfuse 2.x`.
- Desplegar el plugin a perfiles operativos o reiniciar gateways.
- `git push` (requiere aprobación explícita del CEO).
