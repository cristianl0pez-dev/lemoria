# Changelog

All notable changes to Lemoria will be documented in this file.

The format follows Keep a Changelog, and versions follow Semantic Versioning
while the project moves toward public releases.

## [Unreleased]

### Changed
- **El flujo es 100 % agente-driven**: abrís OpenCode en el proyecto y describís
  la feature en el prompt; el orquestador crea proyecto, conversación, PRD y
  tareas, ejecuta los comandos de Lemoria y delega en los subagentes. El humano
  ya no corre `lemoria project create`, `lemoria flow start` ni ningún slash
  command.
- La configuración global de OpenCode (`~/.config/opencode/opencode.json`) define
  `default_agent: orchestrator`, así que abrir OpenCode en cualquier proyecto
  arranca el orquestador.
- El modelo y effort del orquestador son de sesión y se eligen con `/models` y
  `/variants` de OpenCode. Los subagentes se siguen pineando por CLI con
  `lemoria agent model <agente> <provider/model> --effort <effort>`, que escribe
  `model: provider/model#variant` en el `.md`; los agentes sin pinear heredan el
  modelo de la sesión del orquestador.
- El instalador ahora limpia los artefactos del menú anterior en modo global y
  de proyecto: `plugins/lemoria-menu/`, `commands/lemoria.md` (y el directorio
  `commands/` si queda vacío) y la entrada `lemoria-menu` de `cli.json`,
  preservando el resto de plugins y dejando respaldos `.bak` de los JSON que
  modifica.
- `ensure_global_default_agent` crea la config global si no existe o agrega
  `default_agent: orchestrator` a una existente sin pisar otras claves. Si ya
  hay otro `default_agent`, lo respeta y solo advierte.

### Removed
- Plugin del TUI que agregaba el grupo **Lemoria** a la paleta **Ctrl+P**
  (`~/.config/opencode/plugins/lemoria-menu/`), junto con su registro en
  `cli.json`.
- Slash command `/lemoria` (`.opencode/commands/`) y los cuatro comandos JSON de
  `opencode.jsonc`; el archivo ya no tiene la clave `commands`.
- `install.sh` ya no copia `.opencode/commands/` ni instala o registra el plugin
  del menú Ctrl+P.

### Fixed
- `ruff check --fix database/` corrigió 25 errores auto-fixables en 16 archivos
  de `database/models/`: 16 de orden de imports, 5 de imports sin usar, 3 de
  `datetime.timezone.utc` → `datetime.UTC` y 1 de `__all__` desordenado. Sin
  cambios de comportamiento.
- CI: el paso **Lint** ya no ejecuta `pip install ruff` (que pisaba el
  `ruff==0.17.2` fijado en dev extras) y ahora corre `ruff check .` en vez de
  `ruff check lemoria/`, cubriendo también `database/` y `tests/`. Verificación:
  `ruff check .` sin errores y 252 tests pasando.

## [0.3.0] - 2026-09-30

### Added
- `lemoria commit add/list/sync` for commit traceability.
- Historical commit backfill from `Task: <task-id>` trailers.
- Installer logic extracted to a testable shell library with installer tests.
- Support for `uv -> venv -> pip` installation order.
- Community docs: contributing and security policy.

### Changed
- Docker detection now checks the daemon with `docker info`, not only the Compose
  plugin version.
- PostgreSQL native is treated as the preferred path; Docker remains optional.
- Vault `commits.md` now lists registered commits and links them to tasks.
- Omarchy panel changes are included with the contract test that requires them.

### Fixed
- CI failure caused by committing the panel contract test before the matching
  `Panel.qml` implementation.
- `lemoria task create -a` now accepts agent names or UUIDs and fails with a
  readable error instead of a foreign-key traceback.
- `print_db_stop_hint` now handles every database provider state explicitly.
