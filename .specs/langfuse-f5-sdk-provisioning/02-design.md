# 02 — Diseño técnico (F5)

## 1. Layout del store

```
/opt/data/lazy-packages-langfuse4/     # base (symlink /opt/data -> /opt/hermes-agent-data)
└── py314/                             # ABI/interpreter tag: py{major}{minor}
    ├── langfuse/  langfuse-4.15.2.dist-info/
    ├── backoff/ opentelemetry*/ protobuf/ wrapt/ google/ ...
```

Un store por intérprete evita el fallo ya observado en el store compartido (`pydantic_core._pydantic_core` compilado para otro intérprete). El contenido se instala con el **mismo** intérprete que ejecuta Hermes (`uv pip install --python <venv>/bin/python --target <base>/pyXY`) **con** dependencias: es un árbol `--target` completo (~31 MB) que incluye su propio `pydantic`/`pydantic_core`/`httpx`. Lo contractual es sólo el **orden**: al añadirse al final de `sys.path`, donde el intérprete ya trae `pydantic`/`httpx` (el venv de Hermes los trae) gana la copia del venv; en un intérprete que no los traiga, la copia pineada del store es la que hace importable el SDK. No es un store mínimo y no pretende serlo.

## 2. Bootstrap de `sys.path` (`sdk_env.py`)

```python
ensure_sdk_paths() -> tuple[str, ...]   # añade (append) los candidatos existentes, idempotente
sdk_store_candidates() -> list[str]     # HERMES_LANGFUSE_SDK_PATH (pathsep) + <base>/pyXY
```

- Se llama en la cabecera de `__init__.py`, **antes** de `from langfuse import ...`, y en el fallback standalone de `legacy_ingestion.py` (scripts que cargan el adaptador por ruta).
- `sys.path.append`, nunca `insert`: el venv mantiene precedencia de `pydantic`/`httpx`.
- Jamás se añade `/opt/data/lazy-packages` (store 2.x compartido).

### 2.1 Manifiesto: sidecar, no miembro

`plugin.yaml` declara `python_runtime: external`. Con eso `pm/plugin_declarations.py::read_python_declaration()` devuelve
`external=True` y `is_member=False`, así que `hermes pm repair`/install **no** construye `langfuse==4.15.2` en el venv compartido
(el test `test_pm_reads_the_plugin_as_a_non_member` lo verifica contra el propio código de PM, no contra una lectura nuestra).

## 3. Estado degradado visible

`_build_client()` cubre **ambos** transportes (`otlp`, por defecto, y `legacy`, opt-in):

- La excepción real del import del SDK se captura en cabecera de módulo (`_SDK_IMPORT_ERROR`), de modo que el mensaje nombra la causa concreta (`ModuleNotFoundError: No module named 'langfuse'`, o `ImportError: cannot import name 'propagate_attributes'` cuando un SDK 2.x se resuelve desde otro store).
- Registra `_TRANSPORT_STATUS` = `{state: "degraded", reason: "<TipoExcepción>: <mensaje>", python, stores}` — nunca un `reason` genérico del estilo `langfuse SDK unavailable for the otlp transport`.
- Emite **un** WARNING con transporte + causa + stores + comando `scripts/provision_sdk_store.sh` + `HERMES_LANGFUSE_SDK_PATH` + intérprete.
- No se ofrece `hermes plugins install langfuse --force`: ese comando instala **otro** plugin y PM no construye un `python_runtime: external`.
- El resto de excepciones conserva el warning genérico actual.
- Éxito → `state: "ok"` con el store resuelto.
- `transport_health()` copia defensiva del dict (sin credenciales).

## 4. Provisión (script, no automática)

`langfuse-fleet/scripts/provision_sdk_store.sh`:

```
STORE_BASE=${HERMES_LANGFUSE_STORE_BASE:-/opt/data/lazy-packages-langfuse4}
PYTHON=${HERMES_PYTHON:-/opt/hermes-agent/.venv/bin/python}
target="$STORE_BASE/py$($PYTHON -c 'import sys;print(f"{sys.version_info[0]}{sys.version_info[1]}")')"
uv pip install --python "$PYTHON" --target "$target" "langfuse==4.15.2"
```

No se ejecuta en import (prohibido: red en tiempo de carga). Es un paso de despliegue documentado en README.

## 5. Rollback

1. `rm -rf /opt/data/lazy-packages-langfuse4` → el plugin vuelve al comportamiento anterior (degradado, pero ahora con WARNING accionable).
2. `git revert <commit>` en el repo del plugin (bootstrap + health + tests).
3. El venv de Hermes y el store compartido 2.x nunca se tocan, así que no hay rollback que aplicarles.
