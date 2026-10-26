"""Tests for the installer's decision logic.

install.sh is a linear script: it prompts on stdin, writes to $HOME and starts
containers, so it can't be run inside a test. Its decisions live in
installer/lib.sh, and that is what these tests exercise — the same functions the
installer calls, executed in a subshell with a PATH built from scratch.

The sandbox contains only the tools the library really shells out to plus
whatever fake a test drops in, so `docker`, `uv`, `psql`, `pacman` or
`python3` installed on the machine running the suite can never answer on behalf
of a stub. No sudo, no Docker, no network, and nothing outside tmp_path.
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "installer" / "lib.sh"
INSTALLER = REPO_ROOT / "install.sh"
LOCAL_CONFIG = REPO_ROOT / "opencode.jsonc"
BASH = shutil.which("bash")


# `install.sh` legitimately contains "/lemoria" inside paths such as
# `~/.local/share/lemoria/venv`, so a plain substring check would flag it for
# something unrelated. This matches the old slash command as a standalone token.
SLASH_COMMAND = re.compile(r"(?<![\w./~-])/lemoria\b")


def references_slash_command(text: str) -> bool:
    """True when `text` mentions the removed `/lemoria` slash command."""
    return SLASH_COMMAND.search(text) is not None


def strip_jsonc_comments(text: str) -> str:
    """Remove `//` and `/* */` comments without touching string contents.

    A naive regex would cut `https://opencode.ai/config.json` in half, which is
    exactly the schema URL the config carries.
    """
    result = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            following = text[index + 1]
            if following == "/":
                index += 2
                while index < len(text) and text[index] != "\n":
                    index += 1
                continue
            if following == "*":
                index += 2
                while index + 1 < len(text) and not (
                    text[index] == "*" and text[index + 1] == "/"
                ):
                    index += 1
                index += 2
                continue
        result.append(char)
        index += 1
    return "".join(result)


def load_jsonc(text: str) -> dict:
    """Parse the JSON-with-comments dialect opencode.jsonc uses."""
    without_comments = strip_jsonc_comments(text)
    without_trailing_commas = re.sub(r",(\s*[}\]])", r"\1", without_comments)
    return json.loads(without_trailing_commas)


# The only real programs a test can see. `docker`/`uv`/`psql`/`python3` are not
# on this list on purpose: a test that wants one puts a fake there.
# `dirname` is there because install.sh uses it to find its own directory.
COREUTILS = (
    "cat", "chmod", "cp", "dirname", "grep", "head", "ln",
    "mkdir", "rm", "rmdir", "sed", "seq", "sleep", "tail",
)


class Sandbox:
    """A disposable shell environment: fake PATH, fake HOME, scratch CWD."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.home = root / "home"
        self.workdir = root / "work"
        self.stubs = root / "stubs"
        self.log = root / "calls.log"
        self.docker_up_marker = root / "compose-up-was-called"
        for directory in (self.bin, self.home, self.workdir, self.stubs):
            directory.mkdir()

        for tool in COREUTILS:
            real = shutil.which(tool)
            assert real, f"'{tool}' is required to run installer/lib.sh in a test"
            (self.bin / tool).symlink_to(real)

    # ----- building the environment -----

    def add(self, name: str, body: str) -> Path:
        """Put an executable named `name` on the sandbox PATH."""
        return self._write(self.bin / name, body)

    def add_stub(self, name: str, body: str) -> Path:
        """Write an executable that is NOT on the PATH (payload for another fake)."""
        return self._write(self.stubs / name, body)

    def _write(self, path: Path, body: str) -> Path:
        # Unlink first: the name may already be a symlink into /usr/bin, and
        # write_text would follow it and overwrite a real system binary.
        path.unlink(missing_ok=True)
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return path

    def env(self, **extra) -> dict:
        """A from-scratch environment. Nothing from the developer's shell leaks in."""
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "LOG": str(self.log),
            "COMPOSE_UP_MARKER": str(self.docker_up_marker),
            "LEMORIA_DIR": str(self.workdir),
        }
        env.update({key: str(value) for key, value in extra.items()})
        return env

    # ----- running -----

    def run(self, body: str, *, env=None) -> str:
        """Source the library, run `body`, and return everything it printed.

        `set -e` is on because the crash this file guards against was a `set -e`
        abort: a test that ran without it could pass while the real installer
        died. The script must also succeed, or the assertion is meaningless.
        """
        prelude = "set -euo pipefail\n"
        script = f'{prelude}source "{LIB}"\n' + textwrap.dedent(body)
        result = subprocess.run(
            [BASH, "-c", script],
            env=env if env is not None else self.env(),
            cwd=self.workdir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, (
            f"script failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
        return result.stdout

    def run_expecting_failure(self, body: str, *, env=None) -> subprocess.CompletedProcess:
        """Same as run(), but the script is allowed to fail; returns the result."""
        prelude = "set -euo pipefail\n"
        script = f'{prelude}source "{LIB}"\n' + textwrap.dedent(body)
        return subprocess.run(
            [BASH, "-c", script],
            env=env if env is not None else self.env(),
            cwd=self.workdir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    # ----- reading what the fakes recorded -----

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


def fake_docker(sandbox, *, info_rc=0, up_rc=0, healthy_rc=0):
    """A `docker` whose daemon state is controlled, logging every invocation.

    `compose version` exits 0 no matter what, which is the whole point: on a real
    machine it reads the CLI plugin version and never touches the socket, so it
    succeeds even when the daemon is unreachable.
    """
    sandbox.add(
        "docker",
        r"""
        printf 'docker %s\n' "$*" >> "$LOG"
        case "$*" in
            "info")  exit "${FAKE_DOCKER_INFO:-0}" ;;
            "--version") echo "Docker version 0.0.0-fake"; exit 0 ;;
            "compose version") exit 0 ;;
            "compose up -d") : > "$COMPOSE_UP_MARKER"; exit "${FAKE_DOCKER_UP:-0}" ;;
            "compose exec db pg_isready -U lemoria") exit "${FAKE_DOCKER_HEALTHY:-0}" ;;
        esac
        exit 0
        """,
    )
    return sandbox.env(
        FAKE_DOCKER_INFO=info_rc, FAKE_DOCKER_UP=up_rc, FAKE_DOCKER_HEALTHY=healthy_rc
    )


def fake_pg_isready(sandbox, *, rc=0):
    sandbox.add(
        "pg_isready",
        r"""printf 'pg_isready %s\n' "$*" >> "$LOG"; exit "${FAKE_PG_RC:-0}";""",
    )
    return sandbox.env(FAKE_PG_RC=rc)


def fake_psql(sandbox, *, rc=0):
    sandbox.add(
        "psql",
        r"""
        printf 'psql %s\n' "$*" >> "$LOG"
        printf '%s\n' "${PGPASSWORD-}" > "$PSQL_PASSWORD_FILE"
        exit "${FAKE_PSQL:-0}"
        """,
    )
    return sandbox.env(FAKE_PSQL=rc, PSQL_PASSWORD_FILE=str(sandbox.root / "psql-password"))


def fake_uv(sandbox, *, install_rc=0, bin_dir=None):
    sandbox.add(
        "uv",
        r"""
        printf 'uv %s\n' "$*" >> "$LOG"
        case "$1" in
            --version) echo "uv 0.0.0-fake"; exit 0 ;;
            tool)
                if [ "${2:-}" = "dir" ]; then
                    printf '%s\n' "${FAKE_UV_BIN:-$HOME/.local/bin}"
                    exit 0
                fi
                ;;
        esac
        exit "${FAKE_UV_INSTALL:-0}"
        """,
    )
    return sandbox.env(
        FAKE_UV_INSTALL=install_rc,
        FAKE_UV_BIN=bin_dir or str(sandbox.home / ".local/bin"),
    )


def fake_python3(sandbox, *, venv_rc=0, user_install_rc=0, system_install_rc=0):
    """A `python3` that fabricates a venv instead of building a real one."""
    sandbox.add_stub(
        "pip",
        r"""
        printf 'pip %s\n' "$*" >> "$LOG"
        exit "${FAKE_PIP_IN_VENV:-0}"
        """,
    )
    sandbox.add(
        "python3",
        r"""
        printf 'python3 %s\n' "$*" >> "$LOG"
        if [ "${1:-}" = "--version" ]; then
            echo "Python 0.0.0-fake"
            exit 0
        fi
        if [ "$1" = "-m" ] && [ "${2:-}" = "venv" ]; then
            mkdir -p "$3/bin" || exit 1
            cp "$PIP_STUB" "$3/bin/pip" || exit 1
            printf '#!/bin/sh\nexit 0\n' > "$3/bin/lemoria"
            chmod +x "$3/bin/pip" "$3/bin/lemoria"
            exit "${FAKE_PY_VENV:-0}"
        fi
        if [ "$1" = "-m" ] && [ "${2:-}" = "pip" ]; then
            case " $* " in
                *" --user "*)
                    echo "error: externally-managed-environment" >&2
                    exit "${FAKE_PIP_USER:-0}"
                    ;;
            esac
            echo "error: no module named pip" >&2
            exit "${FAKE_PIP_SYSTEM:-0}"
        fi
        exit 0
        """,
    )
    return sandbox.env(
        PIP_STUB=str(sandbox.stubs / "pip"),
        FAKE_PY_VENV=venv_rc,
        FAKE_PIP_USER=user_install_rc,
        FAKE_PIP_SYSTEM=system_install_rc,
        FAKE_PIP_IN_VENV=0,
    )


def real_python3(sandbox):
    """Put the real interpreter on the sandbox PATH.

    The OpenCode config helpers parse JSON with `python3`; a fake that only
    answers `--version` would make them fail for the wrong reason.
    """
    real = shutil.which("python3")
    assert real, "python3 is required to exercise the OpenCode config helpers"
    (sandbox.bin / "python3").unlink(missing_ok=True)
    (sandbox.bin / "python3").symlink_to(real)


def fake_distro(sandbox, *tools):
    """Fake package managers; only `command -v` matters to the library."""
    for tool in tools:
        sandbox.add(tool, "exit 0")


# The bare minimum install.sh needs to get as far as the requirements report.
TRIVIAL_PYTHON3 = 'if [ "$1" = "--version" ]; then echo "Python 3.12.0"; exit 0; fi\nexit 0'


def psql_password(sandbox) -> str:
    return (sandbox.root / "psql-password").read_text().strip()


# Chains that log themselves, so a test can see which strategy ran. The return
# code is what the fake is told to pretend.
STRATEGIES = """
uv_strategy()   { printf 'strategy:uv\\n' >> "$LOG";   return "${STRATEGY_UV_RC:-0}"; }
venv_strategy() { printf 'strategy:venv\\n' >> "$LOG"; return "${STRATEGY_VENV_RC:-0}"; }
pip_strategy()  { printf 'strategy:pip\\n' >> "$LOG";  return "${STRATEGY_PIP_RC:-0}"; }
"""

# The chain, called with the doubles above instead of the real installers.
PICK = "choose_installer uv_strategy venv_strategy pip_strategy"


class TestDockerDetection:
    """Docker has two independent failure modes and the probe must tell them apart."""

    def test_reachable_daemon_makes_docker_usable(self, sandbox):
        env = fake_docker(sandbox, info_rc=0)
        out = sandbox.run("detect_docker; echo \"$DOCKER_BINARIO $DOCKER_USABLE\"", env=env)
        assert out.strip() == "true true"

    def test_unreachable_daemon_makes_docker_not_usable(self, sandbox):
        env = fake_docker(sandbox, info_rc=1)
        out = sandbox.run("detect_docker; echo \"$DOCKER_BINARIO $DOCKER_USABLE\"", env=env)
        assert out.strip() == "true false"

    def test_absent_docker_is_neither_installed_nor_usable(self, sandbox):
        out = sandbox.run("detect_docker; echo \"$DOCKER_BINARIO $DOCKER_USABLE\"")
        assert out.strip() == "false false"
        assert sandbox.calls() == [], "nothing should be executed when docker isn't installed"

    def test_unreachable_daemon_is_rejected_even_though_compose_version_answers(self, sandbox):
        """The bug this whole file exists for.

        `docker compose version` exits 0 on a machine whose socket denies the
        user, because it only reads the CLI plugin version. Treating that exit
        code as "Docker works" sent the installer on to `docker compose up -d`,
        which then died with permission denied under `set -e`.
        """
        env = fake_docker(sandbox, info_rc=1)
        out = sandbox.run("detect_docker; echo \"$DOCKER_USABLE\"", env=env)
        assert out.strip() == "false", "a daemon that cannot be reached is not usable"

    def test_availability_is_never_probed_with_compose_version(self, sandbox):
        env = fake_docker(sandbox, info_rc=1)
        sandbox.run("detect_docker", env=env)
        probes = [call for call in sandbox.calls() if "version" in call]
        assert probes == [], f"'docker compose version' does not talk to the daemon; got {probes}"

    def test_detection_converses_with_the_daemon(self, sandbox):
        env = fake_docker(sandbox, info_rc=0)
        sandbox.run("detect_docker", env=env)
        assert "docker info" in sandbox.calls(), "only a real daemon round-trip proves availability"

    def test_detection_survives_an_unreachable_daemon_under_set_e(self, sandbox):
        env = fake_docker(sandbox, info_rc=1)
        out = sandbox.run('detect_docker; echo "seguí"', env=env)
        assert out.strip() == "seguí", "detection must not abort the installer"


class TestPostgresDetection:
    def test_something_answering_on_the_port_counts_as_ready(self, sandbox):
        env = fake_pg_isready(sandbox, rc=0)
        out = sandbox.run('DB_PORT=5432; detect_postgres; echo "$PG_READY"', env=env)
        assert out.strip() == "true"

    def test_nothing_answering_counts_as_not_ready(self, sandbox):
        env = fake_pg_isready(sandbox, rc=2)
        out = sandbox.run('DB_PORT=5432; detect_postgres; echo "$PG_READY"', env=env)
        assert out.strip() == "false"

    def test_missing_pg_isready_is_not_ready(self, sandbox):
        out = sandbox.run('DB_PORT=5432; detect_postgres; echo "$PG_READY"')
        assert out.strip() == "false"

    def test_detection_asks_about_the_tcp_port_not_only_the_unix_socket(self, sandbox):
        env = fake_pg_isready(sandbox, rc=0)
        sandbox.run('DB_PORT=5432; detect_postgres', env=env)
        assert "pg_isready -h localhost -p 5432" in " ".join(sandbox.calls()), (
            "without -h, pg_isready only checks the socket and says nothing about "
            "a server listening on TCP"
        )

    def test_the_configured_port_is_the_one_that_gets_probed(self, sandbox):
        env = fake_pg_isready(sandbox, rc=0)
        env["LEMORIA_DB_PORT"] = "6543"
        sandbox.run('DB_PORT="$(resolve_db_port)"; detect_postgres', env=env)
        assert any("6543" in call for call in sandbox.calls())

    def test_the_port_defaults_to_5432(self, sandbox):
        assert sandbox.run('resolve_db_port').strip() == "5432"

    def test_the_port_can_be_overridden_from_the_environment(self, sandbox):
        out = sandbox.run('resolve_db_port', env=sandbox.env(LEMORIA_DB_PORT="6543"))
        assert out.strip() == "6543"


class TestDatabaseProvider:
    def test_a_running_postgres_wins_over_a_working_docker(self, sandbox):
        env = fake_docker(sandbox)
        out = sandbox.run('PG_READY=true; DOCKER_USABLE=true; resolve_db_provider', env=env)
        assert out.strip() == "preexistente", "a server already up is not this installer's business"

    def test_docker_provides_the_database_when_nothing_was_running(self, sandbox):
        env = fake_docker(sandbox)
        out = sandbox.run('PG_READY=false; DOCKER_USABLE=true; resolve_db_provider', env=env)
        assert out.strip() == "docker"

    def test_no_provider_when_there_is_neither_postgres_nor_docker(self, sandbox):
        out = sandbox.run('PG_READY=false; DOCKER_USABLE=false; resolve_db_provider')
        assert out.strip() == "ninguno"

    def test_an_unreachable_daemon_is_not_a_database_provider(self, sandbox):
        env = fake_docker(sandbox, info_rc=1)
        out = sandbox.run(
            "DB_PORT=5432; detect_docker; detect_postgres; resolve_db_provider",
            env=env,  # pg_isready is absent from the PATH, so nothing is running
        )
        assert out.strip() == "ninguno"


class TestExistingPostgresIsLeftAlone:
    def test_docker_is_never_started_when_postgres_is_already_answers(self, sandbox):
        env = fake_docker(sandbox)
        env.update(fake_pg_isready(sandbox, rc=0))
        env.update(fake_psql(sandbox))
        out = sandbox.run(
            """
            DB_PORT=5432
            detect_postgres
            detect_docker
            provide_postgres "$DB_PORT" lemoria lemoria lemoria
            echo "provider=$DB_PROVIDER"
            """,
            env=env,
        )
        assert "provider=preexistente" in out
        assert "compose up -d" not in " ".join(sandbox.calls()), (
            "a server already up must not be replaced"
        )
        assert not sandbox.docker_up_marker.exists()

    def test_credentials_from_the_env_file_are_the_ones_that_get_checked(self, sandbox):
        env = fake_psql(sandbox)
        (sandbox.workdir / ".env").write_text("LEMORIA_DB_USER=otro\nLEMORIA_DB_PASSWORD=clave\n")
        out = sandbox.run(
            'DB_PORT=5432; PG_READY=true; DOCKER_USABLE=false; '
            'provide_postgres "$DB_PORT" otro clave basedb; echo "creds=$PG_CREDENTIALS"',
            env=env,
        )
        assert "creds=ok" in out
        assert "psql -h localhost -p 5432 -U otro -d basedb" in " ".join(sandbox.calls())
        assert psql_password(sandbox) == "clave", (
            "the password must be passed via PGPASSWORD, not on argv"
        )

    def test_wrong_credentials_are_reported_instead_of_ignored(self, sandbox):
        env = fake_psql(sandbox, rc=1)
        out = sandbox.run(
            'DB_PORT=5432; PG_READY=true; DOCKER_USABLE=false; '
            'provide_postgres "$DB_PORT" lemoria mal lemoria; echo "creds=$PG_CREDENTIALS"',
            env=env,
        )
        assert "creds=invalid" in out
        assert "no sirven" in out

    def test_missing_psql_leaves_the_credentials_unverified_without_failing(self, sandbox):
        out = sandbox.run(
            'DB_PORT=5432; PG_READY=true; DOCKER_USABLE=false; '
            'provide_postgres "$DB_PORT" lemoria lemoria lemoria; echo "creds=$PG_CREDENTIALS"',
        )
        assert "creds=no-psql" in out
        assert "no pude verificar" in out


class TestStartingPostgresWithDocker:
    def test_a_healthy_stack_is_reported_as_ready(self, sandbox):
        env = fake_docker(sandbox)
        out = sandbox.run('DB_PORT=5432; detect_docker; start_docker_postgres "$DB_PORT"', env=env)
        assert "PostgreSQL (Docker) listo" in out
        assert sandbox.docker_up_marker.exists()

    def test_the_health_loop_polls_the_container_not_the_host(self, sandbox):
        env = fake_docker(sandbox)
        sandbox.run('DB_PORT=5432; detect_docker; start_docker_postgres "$DB_PORT"', env=env)
        assert any("compose exec db pg_isready -U lemoria" in c for c in sandbox.calls())

    def test_a_stack_that_never_comes_up_fails_instead_of_hanging_forever(self, sandbox):
        env = fake_docker(sandbox, healthy_rc=1)
        env["PATH"] = str(sandbox.bin)
        sandbox.add("sleep", "exit 0")  # the 60 retries must not take 60 seconds
        result = sandbox.run_expecting_failure(
            'DB_PORT=5432; detect_docker; start_docker_postgres "$DB_PORT"',
            env=env,
        )
        assert result.returncode != 0
        assert "no quedó listo" in result.stdout
        assert sum("compose exec db pg_isready" in c for c in sandbox.calls()) == 60

    def test_a_failing_compose_up_is_explained_instead_of_crashing(self, sandbox):
        env = fake_docker(sandbox, up_rc=1)
        fake_distro(sandbox, "pacman")
        result = sandbox.run_expecting_failure(
            'DB_PORT=5544; detect_docker; start_docker_postgres "$DB_PORT"',
            env=env,
        )
        assert result.returncode != 0
        assert "otro servicio ya ocupa el puerto 5544" in result.stdout
        assert "sudo pacman -S postgresql" in result.stdout, (
            "the failure must say what to do instead"
        )

    def test_a_daemon_dying_before_compose_up_does_not_kill_the_script(self, sandbox):
        """`set -e` used to abort here with a bare 'permission denied'."""
        env = fake_docker(sandbox, up_rc=1)
        script = f'set -euo pipefail\nsource "{LIB}"\nprovide_postgres 5432 u p d || exit 3'
        result = subprocess.run(
            [BASH, "-c", script],
            env=env,
            cwd=sandbox.workdir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 3, (
            "the installer must fail with its own message, not a raw set -e abort"
        )


class TestStopHint:
    def test_a_pre_existing_server_is_stopped_with_systemd_when_systemd_manages_it(self, sandbox):
        sandbox.add("systemctl", "echo 'postgresql.service enabled'; exit 0")
        out = sandbox.run('DB_PORT=5432; print_db_stop_hint "$DB_PROVIDER_PREEXISTING"')
        assert "sudo systemctl stop postgresql" in out

    def test_a_pre_existing_server_without_systemd_says_so(self, sandbox):
        out = sandbox.run('DB_PORT=5432; print_db_stop_hint "$DB_PROVIDER_PREEXISTING"')
        assert "no lo levanta systemd" in out or "no lo maneja systemd" in out

    def test_a_docker_database_is_stopped_with_compose_down(self, sandbox):
        out = sandbox.run('DB_PORT=5432; print_db_stop_hint "$DB_PROVIDER_DOCKER"')
        assert "docker compose down" in out
        assert "-v" in out, "the user needs to know how to drop the volume too"

    def test_a_provider_with_no_database_does_not_invent_stop_instructions(self, sandbox):
        out = sandbox.run('DB_PORT=5432; print_db_stop_hint "$DB_PROVIDER_NONE"')
        assert "docker compose down" not in out
        assert "systemctl stop" not in out

    def test_every_provider_the_resolver_can_emit_has_its_own_stop_hint(self, sandbox):
        """`DB_PROVIDER` is a contract between two functions, and a typo in either
        one silently sends every user to the "nothing was started" branch: they
        installed nothing, PostgreSQL was already running, and the one thing
        they needed — how to stop it — is the thing the summary refuses to say.
        """
        providers = sandbox.run(
            """
            printf '%s\\n' "$DB_PROVIDER_PREEXISTING" "$DB_PROVIDER_DOCKER" "$DB_PROVIDER_NONE"
            """
        ).split()
        for provider in providers:
            out = sandbox.run(f'DB_PORT=5432; print_db_stop_hint {provider}')
            assert "no quedó levantado por este instalador" not in out, (
                f"resolve_db_provider emits {provider!r} (DB_PROVIDER_* in installer/lib.sh) "
                f"but print_db_stop_hint has no branch spelled that way, so it falls through "
                f"to the 'nothing was started' message. Make the two agree."
            )


class TestInstallerChain:
    def test_uv_is_chosen_when_it_is_available(self, sandbox):
        env = fake_uv(sandbox)
        out = sandbox.run(STRATEGIES + PICK + '; echo "installer=$INSTALLER"', env=env)
        assert "installer=uv" in out
        assert "strategy:venv" not in sandbox.calls(), "venv must not be touched once uv worked"

    def test_venv_is_chosen_when_uv_is_not_installed(self, sandbox):
        out = sandbox.run(STRATEGIES + PICK + '; echo "installer=$INSTALLER"')
        assert "installer=venv" in out
        assert "strategy:uv" not in sandbox.calls()
        assert "uv no está instalado" in out

    def test_venv_takes_over_when_the_uv_install_fails(self, sandbox):
        env = fake_uv(sandbox)
        env["STRATEGY_UV_RC"] = "1"
        out = sandbox.run(
            STRATEGIES + PICK + '; echo "installer=$INSTALLER"',
            env=env,
        )
        assert "installer=venv" in out
        assert "strategy:uv" in sandbox.calls()
        assert "pruebo con venv propio" in out

    def test_pip_is_the_last_resort(self, sandbox):
        env = sandbox.env(STRATEGY_VENV_RC="1")
        out = sandbox.run(
            STRATEGIES + PICK + '; echo "installer=$INSTALLER"',
            env=env,
        )
        assert "installer=pip" in out
        assert sandbox.calls().count("strategy:pip") == 1

    def test_all_three_failing_is_reported_as_no_installer(self, sandbox):
        env = fake_uv(sandbox)
        env.update(STRATEGY_UV_RC="1", STRATEGY_VENV_RC="1", STRATEGY_PIP_RC="1")
        result = sandbox.run_expecting_failure(
            STRATEGIES
            + PICK + ' || true; echo "installer=[$INSTALLER]"',
            env=env,
        )
        assert result.returncode == 0
        assert "installer=[]" in result.stdout, (
            "install.sh prints its manual guide when INSTALLER is empty"
        )
        assert result.stdout.count("strategy:") == 0
        assert sandbox.calls().count("strategy:uv") == 1
        assert sandbox.calls().count("strategy:venv") == 1
        assert sandbox.calls().count("strategy:pip") == 1

    def test_the_uv_bin_directory_is_kept_for_the_path_fixup(self, sandbox):
        env = fake_uv(sandbox, bin_dir="/opt/uv/bin")
        out = sandbox.run(
            STRATEGIES + PICK + '; echo "bin=$UV_BIN_DIR"',
            env=env,
        )
        assert "bin=/opt/uv/bin" in out, (
            "uv installs outside ~/.local/bin; without this the PATH fixup misses it"
        )

    def test_uv_receives_the_dev_extra_in_pep_508_form(self, sandbox):
        env = fake_uv(sandbox)
        sandbox.run('install_with_uv', env=env)
        assert f"uv tool install --editable {sandbox.workdir}[dev]" in sandbox.calls()

    def test_a_venv_install_publishes_the_command_in_local_bin(self, sandbox):
        env = fake_python3(sandbox)
        out = sandbox.run('install_with_venv; echo "rc=$?"', env=env)
        assert "rc=0" in out
        link = sandbox.home / ".local" / "bin" / "lemoria"
        assert link.is_symlink(), (
            "install.sh calls `lemoria init` right after, so the link has to exist"
        )
        assert link.resolve() == (sandbox.home / ".local/share/lemoria/venv/bin/lemoria")
        assert f"pip install -q -e {sandbox.workdir}[dev]" in sandbox.calls()

    def test_a_venv_that_cannot_be_created_fails_without_publishing_anything(self, sandbox):
        env = fake_python3(sandbox, venv_rc=1)
        result = sandbox.run_expecting_failure("install_with_venv", env=env)
        assert result.returncode != 0
        assert not (sandbox.home / ".local" / "bin" / "lemoria").exists()
        assert "no se pudo crear el venv" in result.stdout

    def test_a_failed_venv_install_leaves_no_broken_command_behind(self, sandbox):
        env = fake_python3(sandbox)
        env["FAKE_PIP_IN_VENV"] = "1"
        result = sandbox.run_expecting_failure("install_with_venv", env=env)
        assert result.returncode != 0
        assert not (sandbox.home / ".local" / "bin" / "lemoria").exists()

    def test_pip_retries_without_user_when_the_user_install_is_refused(self, sandbox):
        """PEP 668 environments reject --user first; the retry has to happen."""
        env = fake_python3(sandbox, user_install_rc=1)
        out = sandbox.run('install_with_pip; echo "rc=$?"', env=env)
        assert "rc=0" in out
        attempts = [c for c in sandbox.calls() if c.startswith("python3 -m pip install")]
        assert len(attempts) == 2
        assert "--user" in attempts[0] and "--user" not in attempts[1]

    def test_a_pip_failure_reaches_the_user_instead_of_being_swallowed(self, sandbox):
        """install_with_pip has no `2>/dev/null` on purpose: a pip that died on
        PEP 668 has to look different from one that worked, or the whole chain
        fails with no reason and no hint.
        """
        env = fake_python3(sandbox, user_install_rc=1, system_install_rc=1)
        result = sandbox.run_expecting_failure("install_with_pip", env=env)
        assert result.returncode != 0
        assert "externally-managed-environment" in result.stderr, (
            "pip's own error was hidden from the user"
        )


class TestPackageHints:
    def test_arch_is_told_the_pipx_package_that_actually_exists(self, sandbox):
        fake_distro(sandbox, "pacman")
        assert sandbox.run("pkg_hint python-pipx pipx").strip() == "sudo pacman -S python-pipx"

    def test_debian_is_told_the_pipx_package_that_actually_exists(self, sandbox):
        fake_distro(sandbox, "apt-get")
        assert sandbox.run("pkg_hint python-pipx pipx").strip() == "sudo apt install pipx"

    def test_arch_is_told_that_venv_comes_with_python(self, sandbox):
        fake_distro(sandbox, "pacman")
        hint = sandbox.run("pkg_hint python python3-venv").strip()
        assert hint == "sudo pacman -S python", "on Arch the venv module ships in `python`"

    def test_debian_is_told_to_install_the_venv_module(self, sandbox):
        fake_distro(sandbox, "apt-get")
        hint = sandbox.run("pkg_hint python python3-venv").strip()
        assert hint == "sudo apt install python3-venv"

    def test_an_unknown_distro_gets_both_names_instead_of_a_wrong_command(self, sandbox):
        hint = sandbox.run("pkg_hint python-pipx pipx").strip()
        assert "python-pipx" in hint and "pipx" in hint
        assert "sudo" not in hint, "there is no sudo line to offer when the manager is unknown"

    def test_arch_can_install_uv_from_its_own_repositories(self, sandbox):
        fake_distro(sandbox, "pacman")
        assert sandbox.run("uv_install_hint").strip() == "sudo pacman -S uv"

    def test_everywhere_else_uv_points_at_its_own_installation_docs(self, sandbox):
        hint = sandbox.run("uv_install_hint").strip()
        assert "docs.astral.sh/uv" in hint


class TestPostgresSetupGuide:
    def test_arch_gets_the_pacman_command(self, sandbox):
        fake_distro(sandbox, "pacman")
        out = sandbox.run("DB_PORT=5432; DOCKER_BINARIO=false; print_postgres_setup_guide")
        assert "sudo pacman -S postgresql" in out
        assert "apt install" not in out, (
            "an apt command on Arch sends the user to a binary that isn't there"
        )

    def test_debian_gets_the_apt_command(self, sandbox):
        fake_distro(sandbox, "apt-get")
        out = sandbox.run("DB_PORT=5432; DOCKER_BINARIO=false; print_postgres_setup_guide")
        assert "sudo apt install postgresql" in out

    def test_a_docker_binary_with_a_dead_daemon_is_explained_not_reinstalled(self, sandbox):
        env = fake_docker(sandbox, info_rc=1)
        fake_distro(sandbox, "pacman")
        out = sandbox.run("DB_PORT=5432; detect_docker; print_postgres_setup_guide", env=env)
        assert "docker info" in out, (
            "the guide should point at the command that shows the real state"
        )
        assert "docker.sock" in out, "socket permissions are the typical cause"
        assert "Instalá Docker primero" not in out, (
            "they already have Docker; telling them to install it wastes their time"
        )

    def test_no_docker_at_all_means_docker_has_to_be_installed(self, sandbox):
        fake_distro(sandbox, "pacman")
        out = sandbox.run("DB_PORT=5432; DOCKER_BINARIO=false; print_postgres_setup_guide")
        assert "sudo pacman -S docker" in out

    def test_the_guide_warns_that_the_docker_group_is_equivalent_to_root(self, sandbox):
        fake_distro(sandbox, "pacman")
        out = sandbox.run("DB_PORT=5432; DOCKER_BINARIO=false; print_postgres_setup_guide")
        assert "usermod -aG docker" in out
        assert "equivalente a root" in out
        assert "newgrp docker" in out
        assert "antes de reintentar ./install.sh" in out

    def test_the_guide_names_the_port_it_checked(self, sandbox):
        fake_distro(sandbox, "pacman")
        out = sandbox.run("DB_PORT=5544; DOCKER_BINARIO=false; print_postgres_setup_guide")
        assert "5544" in out


class TestCleanupLegacyOpenCodeUI:
    """An upgrade must remove 0.3.0's Ctrl+P menu and slash command.

    Only Lemoria's own entries may disappear: the user's plugins, commands and
    the rest of their cli.json stay exactly as they were.
    """

    def _legacy_dir(self, sandbox, *, cli_json: str | None = None) -> Path:
        opencode = sandbox.root / "opencode"
        plugin = opencode / "plugins" / "lemoria-menu"
        plugin.mkdir(parents=True)
        (plugin / "tui.js").write_text("export default { id: 'lemoria.menu' }\n")
        commands = opencode / "commands"
        commands.mkdir()
        (commands / "lemoria.md").write_text("# lemoria\n")
        (commands / "keep.md").write_text("# keep\n")
        if cli_json is None:
            cli_json = (
                json.dumps(
                    {
                        "$schema": "https://opencode.ai/v2/cli.json",
                        "plugins": ["/tmp/other", str(plugin)],
                        "theme": {"name": "dark"},
                    },
                    indent=2,
                )
                + "\n"
            )
        (opencode / "cli.json").write_text(cli_json)
        return opencode

    def test_removes_the_legacy_ui_and_preserves_user_configuration(self, sandbox):
        real_python3(sandbox)
        opencode = self._legacy_dir(sandbox)
        original = (opencode / "cli.json").read_text()

        sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')

        assert not (opencode / "plugins" / "lemoria-menu").exists()
        assert not (opencode / "commands" / "lemoria.md").exists()
        assert (opencode / "commands" / "keep.md").exists()
        assert (opencode / "commands").is_dir(), "other commands keep the directory alive"
        config = json.loads((opencode / "cli.json").read_text())
        assert config["plugins"] == ["/tmp/other"]
        assert config["theme"] == {"name": "dark"}
        assert config["$schema"] == "https://opencode.ai/v2/cli.json"
        assert (opencode / "cli.json.bak").read_text() == original

    def test_invalid_cli_json_keeps_legacy_artifacts_to_avoid_dangling_registration(self, sandbox):
        """If cli.json cannot be cleaned, removing the files would leave it
        pointing at a plugin that no longer exists. Keep both until it can.
        """
        real_python3(sandbox)
        invalid = "{not json"
        opencode = self._legacy_dir(sandbox, cli_json=invalid)

        out = sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')

        assert (opencode / "plugins" / "lemoria-menu" / "tui.js").exists(), (
            "a dangling cli.json registration would outlive the plugin directory"
        )
        assert (opencode / "commands" / "lemoria.md").exists()
        assert (opencode / "cli.json").read_text() == invalid
        assert (opencode / "cli.json.bak").read_text() == invalid
        assert "no es JSON válido" in out
        assert "No se quitan el plugin ni el comando" in out

    def test_only_lemoria_command_leaves_the_commands_directory_empty(self, sandbox):
        real_python3(sandbox)
        opencode = sandbox.root / "opencode"
        commands = opencode / "commands"
        commands.mkdir(parents=True)
        (commands / "lemoria.md").write_text("# lemoria\n")

        sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')

        assert not (commands / "lemoria.md").exists()
        assert not commands.exists(), "an empty commands directory must not be left behind"

    def test_cli_json_with_non_list_plugins_removes_artifacts_without_backup(self, sandbox):
        real_python3(sandbox)
        original = json.dumps({"plugins": "oops", "theme": {"name": "dark"}}, indent=2) + "\n"
        opencode = self._legacy_dir(sandbox, cli_json=original)

        sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')

        assert not (opencode / "plugins" / "lemoria-menu").exists()
        assert not (opencode / "commands" / "lemoria.md").exists()
        assert (opencode / "cli.json").read_text() == original
        assert not (opencode / "cli.json.bak").exists()

    def test_second_run_is_idempotent_and_does_not_churn_backups(self, sandbox):
        real_python3(sandbox)
        opencode = self._legacy_dir(sandbox)
        sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')
        cli = opencode / "cli.json"
        backup = opencode / "cli.json.bak"
        first_cli = (cli.read_text(), cli.stat().st_ino, cli.stat().st_mtime_ns)
        first_backup = (backup.read_text(), backup.stat().st_ino, backup.stat().st_mtime_ns)

        out = sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')

        assert out == "", "a clean directory must not produce noise"
        assert (cli.read_text(), cli.stat().st_ino, cli.stat().st_mtime_ns) == first_cli
        assert (
            backup.read_text(), backup.stat().st_ino, backup.stat().st_mtime_ns
        ) == first_backup, "the second run must not overwrite the backup"

    def test_invalid_cli_json_is_left_byte_identical_with_a_backup(self, sandbox):
        real_python3(sandbox)
        opencode = sandbox.root / "opencode"
        opencode.mkdir()
        invalid = '{"plugins": ['
        (opencode / "cli.json").write_text(invalid)

        out = sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')

        assert (opencode / "cli.json").read_text() == invalid
        assert (opencode / "cli.json.bak").read_text() == invalid
        assert "no es JSON válido" in out

    def test_an_empty_directory_is_silent_and_creates_nothing(self, sandbox):
        real_python3(sandbox)
        opencode = sandbox.root / "opencode"
        opencode.mkdir()

        out = sandbox.run(f'cleanup_legacy_opencode_ui "{opencode}"')

        assert out == ""
        assert list(opencode.iterdir()) == []


class TestEnsureGlobalDefaultAgent:
    """The global config gains our default agent without losing user settings."""

    def _config_path(self, sandbox) -> Path:
        opencode = sandbox.root / "opencode"
        opencode.mkdir(exist_ok=True)
        return opencode / "opencode.json"

    def test_creates_the_config_when_it_does_not_exist(self, sandbox):
        opencode = sandbox.root / "opencode"
        opencode.mkdir()

        out = sandbox.run(f'ensure_global_default_agent "{opencode}"')

        config = json.loads((opencode / "opencode.json").read_text())
        assert config == {
            "$schema": "https://opencode.ai/config.json",
            "default_agent": "orchestrator",
            "skills": ["~/.config/opencode/skills"],
        }
        assert "Config global creada" in out
        assert not (opencode / "opencode.json.bak").exists()

    def test_creates_the_config_and_missing_parent_directories(self, sandbox):
        opencode = sandbox.root / "deeply" / "nested" / "opencode"

        out = sandbox.run(f'ensure_global_default_agent "{opencode}"')

        assert opencode.is_dir()
        config = json.loads((opencode / "opencode.json").read_text())
        assert config["default_agent"] == "orchestrator"
        assert "Config global creada" in out

    def test_adds_the_default_agent_without_losing_other_keys(self, sandbox):
        real_python3(sandbox)
        path = self._config_path(sandbox)
        original = {
            "$schema": "https://opencode.ai/config.json",
            "theme": "dark",
            "skills": ["/custom/skills"],
        }
        original_text = json.dumps(original, indent=2) + "\n"
        path.write_text(original_text)

        out = sandbox.run(f'ensure_global_default_agent "{path.parent}"')

        config = json.loads(path.read_text())
        assert config["default_agent"] == "orchestrator"
        assert config["theme"] == "dark"
        assert config["skills"] == ["/custom/skills"]
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert (path.parent / "opencode.json.bak").read_text() == original_text
        assert "se agregó" in out

    def test_respects_a_different_default_agent_and_reports_it(self, sandbox):
        real_python3(sandbox)
        path = self._config_path(sandbox)
        original_text = json.dumps({"default_agent": "build", "theme": "dark"}, indent=2) + "\n"
        path.write_text(original_text)

        out = sandbox.run(f'ensure_global_default_agent "{path.parent}"')

        assert path.read_text() == original_text, "the user's choice wins"
        assert not (path.parent / "opencode.json.bak").exists()
        assert "build" in out
        assert "se respeta" in out

    def test_orchestrator_already_present_is_a_noop(self, sandbox):
        real_python3(sandbox)
        path = self._config_path(sandbox)
        original_text = json.dumps(
            {"default_agent": "orchestrator", "theme": "dark"}, indent=2
        ) + "\n"
        path.write_text(original_text)
        before = (path.stat().st_ino, path.stat().st_mtime_ns)

        out = sandbox.run(f'ensure_global_default_agent "{path.parent}"')

        assert path.read_text() == original_text
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == before
        assert not (path.parent / "opencode.json.bak").exists()
        assert "ya era" in out

    def test_invalid_json_is_reported_and_left_untouched(self, sandbox):
        real_python3(sandbox)
        path = self._config_path(sandbox)
        invalid = '{"default_agent": '
        path.write_text(invalid)

        out = sandbox.run(f'ensure_global_default_agent "{path.parent}"')

        assert path.read_text() == invalid
        assert "no es JSON válido" in out


class TestOpenCodeCommandInstallContract:
    """The installer installs agents and skills, not a slash command or menu.

    Cleanup helpers live in installer/lib.sh, so these checks target install
    actions rather than the bare appearance of a word.
    """

    def test_installer_does_not_install_slash_commands(self):
        text = INSTALLER.read_text()
        assert "cp .opencode/commands" not in text
        assert 'mkdir -p "$OPENCODE_GLOBAL_DIR/commands"' not in text
        assert not references_slash_command(text)

    def test_installer_does_not_install_the_removed_menu_plugin(self):
        text = INSTALLER.read_text()
        assert "install_opencode_lemoria_menu" not in text
        assert "opencode/plugins/lemoria-menu" not in text
        assert "Plugin habilitado" not in text

    def test_installer_still_copies_agents_and_skills(self):
        text = INSTALLER.read_text()
        assert 'cp .opencode/agents/*.md "$OPENCODE_GLOBAL_DIR/agents/"' in text
        assert 'cp ".opencode/skills/$skill_dir/SKILL.md"' in text


class TestRemovedMenuAndSlashCommand:
    """Regression coverage: the Ctrl+P menu and the slash command are gone.

    Neither the repository nor the installer may quietly bring them back.
    """

    def test_menu_plugin_source_does_not_exist(self):
        assert not (REPO_ROOT / "opencode" / "plugins" / "lemoria-menu").exists()

    def test_local_config_has_no_commands_key(self):
        config = load_jsonc(LOCAL_CONFIG.read_text())
        assert "commands" not in config

    def test_slash_command_directory_does_not_exist(self):
        commands = REPO_ROOT / ".opencode" / "commands"
        assert not (commands / "lemoria.md").exists()
        assert not commands.exists()

    def test_library_does_not_define_the_removed_menu_function(self):
        assert "install_opencode_lemoria_menu" not in LIB.read_text()


class TestLibraryContract:
    """The extraction itself has to hold up.

    A shell function that install.sh calls but the library doesn't define only
    fails when a real user runs the installer, which is exactly the kind of
    breakage this suite exists to prevent.
    """

    ENTRY_POINTS = (
        "choose_installer",
        "cleanup_legacy_opencode_ui",
        "detect_docker",
        "detect_postgres",
        "ensure_global_default_agent",
        "env_value",
        "pkg_hint",
        "print_db_stop_hint",
        "print_postgres_setup_guide",
        "provide_postgres",
        "resolve_db_port",
        "uv_install_hint",
    )

    def test_every_function_install_sh_calls_is_defined(self, sandbox):
        out = sandbox.run(
            "for fn in " + " ".join(self.ENTRY_POINTS) + """
do
    declare -F "$fn" >/dev/null || echo "missing:$fn"
done"""
        )
        assert out.strip() == "", (
            f"install.sh calls functions installer/lib.sh does not define:\n{out}"
        )

    def test_sourcing_the_library_runs_nothing(self, sandbox):
        """Otherwise the detection would happen twice, and the flags install.sh
        printed would be about a different moment than the ones it acts on.
        """
        out = sandbox.run('echo "sourced"')
        assert out.strip() == "sourced"
        assert sandbox.calls() == [], "the library must only define functions when sourced"


class TestInstallScriptItself:
    """The only tests that run the real install.sh.

    They stop at the "no database anywhere" abort, which is before the first
    prompt and before anything is written, so they need no sudo, no Docker and
    no network. HOME is a tmp dir, so even a future line that writes something
    cannot touch the machine running the suite.
    """

    def _run(self, sandbox, env=None):
        return subprocess.run(
            [BASH, str(INSTALLER)],
            env=env if env is not None else sandbox.env(),
            cwd=sandbox.workdir,
            capture_output=True,
            text=True,
            input="",
            timeout=60,
            check=False,
        )

    def test_it_aborts_with_a_setup_guide_when_no_database_answers(self, sandbox):
        sandbox.add("python3", TRIVIAL_PYTHON3)
        fake_distro(sandbox, "pacman")
        result = self._run(sandbox)
        assert result.returncode == 1
        assert "postgres: NO ENCONTRADO" in result.stdout
        assert "sudo pacman -S postgresql" in result.stdout
        assert list(sandbox.home.iterdir()) == [], "an aborted install must not touch $HOME"

    def test_a_docker_binary_with_a_dead_daemon_aborts_with_a_guide(self, sandbox):
        """The reported crash, end to end.

        The daemon refuses the socket while `docker compose version` still exits
        0. The old detection believed it and walked into `docker compose up -d`,
        which died with permission denied and no explanation. The run has to stop
        with the setup guide instead.
        """
        sandbox.add("python3", TRIVIAL_PYTHON3)
        fake_distro(sandbox, "pacman")
        env = fake_docker(sandbox, info_rc=1)
        result = self._run(sandbox, env=env)
        assert result.returncode == 1
        assert "daemon NO responde" in result.stdout, "the user must see WHICH docker state failed"
        assert "docker.sock" in result.stdout, "the guide names the actual cause"
        assert "permission denied" not in result.stderr
        assert not sandbox.docker_up_marker.exists(), (
            "nothing may be started on a daemon we can't reach"
        )

    def test_it_does_not_ask_anything_before_it_knows_there_is_a_database(self, sandbox):
        """A prompt on stdin with no database is a dead end: the user answers it
        and then gets an abort.
        """
        sandbox.add("python3", TRIVIAL_PYTHON3)
        fake_distro(sandbox, "pacman")
        result = self._run(sandbox)
        assert "¿Cómo quieres instalar" not in result.stdout
        assert "¿Instalar? [s/N]" not in result.stdout


class TestEnvValue:
    """The installer must read .env without running what is inside it."""

    def _write_env(self, sandbox, content: str) -> None:
        (sandbox.workdir / ".env").write_text(content)

    def test_a_key_is_read_from_the_file(self, sandbox):
        self._write_env(sandbox, "LEMORIA_DB_USER=lector\n")
        assert sandbox.run("env_value LEMORIA_DB_USER lemoria").strip() == "lector"

    def test_a_missing_key_falls_back(self, sandbox):
        self._write_env(sandbox, "LEMORIA_DB_USER=lector\n")
        assert sandbox.run("env_value LEMORIA_DB_PASSWORD lemoria").strip() == "lemoria"

    def test_quoted_values_keep_their_inner_spaces(self, sandbox):
        self._write_env(sandbox, 'LEMORIA_DB_PASSWORD="  con espacios  "\n')
        out = sandbox.run('printf "[%s]" "$(env_value LEMORIA_DB_PASSWORD x)"')
        assert out.strip() == "[  con espacios  ]"

    def test_unquoted_values_lose_an_inline_comment(self, sandbox):
        self._write_env(sandbox, "LEMORIA_DB_NAME=lemoria # la base de siempre\n")
        assert sandbox.run("env_value LEMORIA_DB_NAME x").strip() == "lemoria"

    def test_a_trailing_space_cannot_hide_inside_an_unquoted_value(self, sandbox):
        self._write_env(sandbox, "LEMORIA_DB_PASSWORD=secreto   \n")
        assert sandbox.run("env_value LEMORIA_DB_PASSWORD x").strip() == "secreto"

    def test_the_last_occurrence_of_a_key_wins(self, sandbox):
        self._write_env(sandbox, "LEMORIA_DB_USER=primero\nLEMORIA_DB_USER=ultimo\n")
        assert sandbox.run("env_value LEMORIA_DB_USER x").strip() == "ultimo"

    def test_the_file_is_parsed_not_executed(self, sandbox):
        """Sourcing .env would run whatever a repository put in it, the moment
        before the installer gives that repository root on the machine.
        """
        marker = sandbox.root / "pwned"
        self._write_env(
            sandbox,
            f"LEMORIA_DB_PASSWORD=$(touch {marker})\n"
            f"LEMORIA_DB_NAME=`touch {marker}`\n",
        )
        out = sandbox.run("echo \"[$(env_value LEMORIA_DB_PASSWORD x)]\"")
        assert not marker.exists(), "reading .env must not run command substitution"
        assert "$(touch" in out, "the raw text is the value, not its result"
