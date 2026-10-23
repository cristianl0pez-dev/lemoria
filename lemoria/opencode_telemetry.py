"""Read-only telemetry from opencode's own database.

opencode records every session in ``$XDG_DATA_HOME/opencode/opencode.db``:
which agent ran it, which model, the token split and the cost. That is the
same data the orchestrator is asked about ("what is running, on what model,
how much has it spent"), so there is no reason to make the agents re-report it
over the SDD ledger.

The file is large (hundreds of MB on an active machine) and opencode writes to
it while we read, so every connection is read-only and every aggregate is
pushed into SQL. opencode owns this schema and may change it between
versions, so the columns are feature-detected and a missing one degrades the
report instead of raising.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .budget import month_start_ms

# What the report still needs from `session`. It used to include the token
# columns and time_created; those moved to `message`, which knows when the
# tokens were spent rather than when the session began. Requiring them here
# would reject a database that is perfectly readable.
_SESSION_COLUMNS = {"agent", "cost", "parent_id", "time_updated"}

# A session touched within this window counts as in flight. opencode has no
# explicit "running" flag, so recency is the honest signal.
ACTIVE_WINDOW_SECONDS = 300


@dataclass
class ModelBucket:
    """One model's totals, in the four buckets the panel's contract names.

    ``reasoning`` is tracked because the total reported to the CLI counts it,
    but the panel has no field for it: its contract is four buckets, so the
    panel's own sum is lower than `total_tokens` by exactly the reasoning
    tokens. Widening the contract is Omarchy's call, not ours.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0

    @property
    def tokens(self) -> int:
        """Everything this bucket spent.

        Written out by hand in three places, one of which forgot the
        reasoning tokens, and the per-model line stopped adding up to the
        total. One definition instead.
        """
        return (
            self.input_tokens + self.output_tokens + self.reasoning
            + self.cache_read + self.cache_write
        )

    def as_contract(self) -> dict:
        """Shape the Omarchy panel expects for `modelUsage`."""
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadInputTokens": self.cache_read,
            "cacheCreationInputTokens": self.cache_write,
        }


@dataclass
class AgentUsage:
    name: str
    sessions: int = 0
    subagent_sessions: int = 0
    active_sessions: int = 0
    tokens: int = 0
    cost: float = 0.0
    model: str | None = None
    variant: str | None = None
    prompts: int = 0
    # What this agent spent since midnight. Without it the panel can only show
    # an all-time number per agent, which is not a number anyone acts on.
    today_tokens: int = 0
    today_prompts: int = 0
    # Every model this agent has used, heaviest first. An agent is not pinned
    # to one model: the orchestrator runs on whatever the user picked, and a
    # subagent can be switched mid-session. Reporting a single "model" for an
    # agent is how a wrong answer becomes invisible. ``model`` is the one used
    # last, not the heaviest: "what is it running" is about recency.
    models: dict = field(default_factory=dict)


@dataclass
class Telemetry:
    """Everything the CLI and the panel record need."""

    available: bool = False
    reason: str = ""
    total_sessions: int = 0
    subagent_sessions: int = 0
    total_prompts: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    today_sessions: int = 0
    today_prompts: int = 0
    today_tokens: int = 0
    today_by_model: dict = field(default_factory=dict)
    # Since midnight on the 1st, local. The window a monthly budget is measured
    # against, kept separate from "today": they reset on different days.
    month_tokens: int = 0
    recent_days: list = field(default_factory=list)
    by_model: dict = field(default_factory=dict)
    by_agent: dict = field(default_factory=dict)
    active_dates: list = field(default_factory=list)


def default_db_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(data_home) / "opencode" / "opencode.db"


# Token accounting lives on messages, not on `session`. The session table only
# carries a running total stamped with the session's *creation* time, which
# makes a session that starts at 23:31 and runs past midnight report nothing at
# all for the new day. Every message has its own timestamp and its own token
# split, so attributing per message is both exact and cheap. The two agree
# exactly in total: summing the message rows reproduces the session totals.
_MESSAGE_TOKEN_SUM = (
    "COALESCE(json_extract(m.data,'$.tokens.input'),0)"
    " + COALESCE(json_extract(m.data,'$.tokens.output'),0)"
    " + COALESCE(json_extract(m.data,'$.tokens.reasoning'),0)"
    " + COALESCE(json_extract(m.data,'$.tokens.cache.read'),0)"
    " + COALESCE(json_extract(m.data,'$.tokens.cache.write'),0)"
)
# opencode v2 moved turns to `session_message` and made the model a nested
# object. Legacy `message` rows keep providerID/modelID at the top level.
_MESSAGE_MODEL_V2 = (
    "CASE WHEN json_type(data,'$.model') = 'object'"
    " THEN json_extract(data,'$.model.providerID') || '/' || json_extract(data,'$.model.id')"
    " ELSE json_extract(data,'$.providerID') || '/' || json_extract(data,'$.modelID')"
    " END"
)
_MESSAGE_MODEL_LEGACY = (
    "json_extract(data,'$.providerID') || '/' || json_extract(data,'$.modelID')"
)
# Both schemas are projected onto this shape so every aggregate stays SQL and
# only has to know one dialect.
_MESSAGE_COLUMNS = "id, session_id, time_created, data, type, model"
# Only assistant turns consume tokens; user rows carry zeroes. v2 records the
# role in a dedicated `type` column rather than inside the JSON payload.
_ASSISTANT = "m.type = 'assistant'"


def _message_cte(tables: set[str]) -> str:
    """Normalize `session_message` (v2) and `message` (legacy) into one source.

    A v2 build still keeps the legacy table for history opencode copied but
    never deleted. The same id can appear in both, so only legacy rows that v2
    does not own are added: counting a duplicated id twice would inflate every
    total.
    """
    v2 = (
        f"SELECT id, session_id, time_created, data, type, {_MESSAGE_MODEL_V2}"
        " FROM session_message"
    )
    legacy = (
        "SELECT id, session_id, time_created, data, json_extract(data,'$.role'),"
        f" {_MESSAGE_MODEL_LEGACY} FROM message"
    )
    if "session_message" in tables:
        branches = [v2]
        if "message" in tables:
            branches.append(
                f"{legacy} WHERE id NOT IN (SELECT id FROM session_message)"
            )
    else:
        branches = [legacy]
    return f"WITH src({_MESSAGE_COLUMNS}) AS ({' UNION ALL '.join(branches)}) "


def _session_source(tables: set[str]) -> str:
    """The session table, with legacy rows added when v2 has taken over.

    v2 sessions live in `session_v2`; `session` keeps the pre-v2 rows. Both
    carry the same four facts the report needs.
    """
    columns = "id, agent, cost, parent_id, time_updated"
    if "session_v2" not in tables:
        return "session"
    v2 = f"SELECT {columns} FROM session_v2"
    if "session" not in tables:
        return f"({v2}) AS session"
    return (
        f"({v2} UNION ALL SELECT {columns} FROM session"
        " WHERE id NOT IN (SELECT id FROM session_v2)) AS session"
    )


def _epoch_ms(days_ago: int = 0) -> int:
    """Start of the day boundary, in *local* time.

    The panel buckets by the user's calendar day, and so does Omarchy's own
    collector (datetime.now().date()). Using UTC here would file a morning
    session under the previous day for anyone west of Greenwich, which is
    exactly where this is being read.
    """
    # Naive local time on purpose: see the docstring.
    when = datetime.now().astimezone().replace(tzinfo=None) - timedelta(days=days_ago)
    start_of_day = when.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start_of_day.timestamp() * 1000)


def _model_id(raw: str | None) -> str:
    """`model` is a JSON blob like {"id":"big-pickle","providerID":"opencode"}.

    Fall back to a tolerant read for a schema that stores a bare string.
    """
    if not raw:
        return "unknown"
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        model_id = str(parsed.get("id") or "unknown")
        provider = str(parsed.get("providerID") or "")
        return f"{provider}/{model_id}" if provider else model_id
    return str(raw).rstrip("/").split("/")[-1] or "unknown"


def _model_variant(raw: str | None) -> str | None:
    try:
        parsed = json.loads(raw or "")
    except (ValueError, TypeError):
        return None
    return parsed.get("variant") if isinstance(parsed, dict) else None


class OpenCodeTelemetry:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()

    def _connect(self) -> sqlite3.Connection:
        # mode=ro plus query_only guarantees a read can never block opencode's
        # writer or leave a WAL/journal file behind.
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.execute("PRAGMA query_only = ON")
        return conn

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    def _missing_columns(self, conn: sqlite3.Connection) -> set[str]:
        """What this opencode build lacks for us to read it at all.

        A message table is not optional: token accounting, the per-day chart
        and the per-agent model split all come from it, because a session row
        only carries a total stamped with the session's start. v2 renamed that
        table to `session_message` and the session table to `session_v2`; a
        build with either pair is readable. Anything else gets the plain
        "schema changed" report instead of a raw "no such table" from
        whichever query happened to run first.
        """
        tables = self._tables(conn)
        if "session_message" not in tables and "message" not in tables:
            return {"message table"}
        session_table = (
            "session_v2" if "session_v2" in tables
            else "session" if "session" in tables
            else None
        )
        if session_table is None:
            return {"session table"}
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({session_table})")}
        return _SESSION_COLUMNS - have

    def read(self) -> Telemetry:
        telemetry = Telemetry()
        if not self.db_path.is_file():
            telemetry.reason = f"no opencode database at {self.db_path}"
            return telemetry

        try:
            conn = self._connect()
        except sqlite3.Error as error:
            telemetry.reason = f"cannot open opencode database: {error}"
            return telemetry

        try:
            missing = self._missing_columns(conn)
            if missing:
                # Degrade rather than raise: a future opencode schema should
                # cost us the report, not crash the command.
                telemetry.reason = f"opencode schema changed, missing: {', '.join(sorted(missing))}"
                return telemetry
            self._read_into(conn, telemetry)
            telemetry.available = True
        except sqlite3.Error as error:
            telemetry.reason = f"opencode database error: {error}"
        finally:
            conn.close()
        return telemetry

    def _read_into(self, conn: sqlite3.Connection, out: Telemetry) -> None:
        # Session-level facts still come from `session`: it is the only place
        # that knows how many sessions ran and which were subagent runs.
        # Everything measured in *tokens* comes from `message` instead, because
        # only a message knows when its tokens were spent and which model spent
        # them. Reading tokens off `session` attributes a whole session to the
        # day it started, so a session that crosses midnight reports nothing for
        # the new day and a multi-day session dumps its whole total on day one.
        # Cost is the one number still read off `session`, because opencode
        # keeps it as a session-level rollup and the per-agent cost is summed
        # from the same place. Tokens are the opposite case: the session only
        # holds a total, so they come per message.
        today = _epoch_ms()
        month = month_start_ms()
        week_ago = _epoch_ms(6)
        active_cutoff = int((time.time() - ACTIVE_WINDOW_SECONDS) * 1000)
        tables = self._tables(conn)
        messages = _message_cte(tables)
        sessions = _session_source(tables)

        row = conn.execute(
            "SELECT COUNT(*),"
            " COALESCE(SUM(CASE WHEN parent_id IS NOT NULL THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(cost), 0)"
            f" FROM {sessions}"
        ).fetchone()
        out.total_sessions, out.subagent_sessions, out.total_cost = row
        out.total_prompts = self._count_prompts(conn, messages)

        # All-time tokens, summed per message.
        out.total_tokens = conn.execute(
            f"{messages} SELECT COALESCE(SUM({_MESSAGE_TOKEN_SUM}), 0)"
            f" FROM src m WHERE {_ASSISTANT}"
        ).fetchone()[0]

        # Today, by the same measure. This is the number that used to read zero
        # for anyone working past midnight.
        row = conn.execute(
            f"{messages} SELECT COALESCE(SUM({_MESSAGE_TOKEN_SUM}), 0) FROM src m"
            f" WHERE {_ASSISTANT} AND m.time_created >= ?",
            (today,),
        ).fetchone()
        out.today_tokens = row[0]
        out.today_prompts = self._count_prompts(conn, messages, since=today)
        out.month_tokens = conn.execute(
            f"{messages} SELECT COALESCE(SUM({_MESSAGE_TOKEN_SUM}), 0)"
            f" FROM src m WHERE {_ASSISTANT} AND m.time_created >= ?",
            (month,),
        ).fetchone()[0]
        out.today_sessions = int(conn.execute(
            f"{messages} SELECT COUNT(DISTINCT session_id) FROM src"
            " WHERE time_created >= ?",
            (today,),
        ).fetchone()[0])

        # Tokens per day, keyed by local date, for the panel's 7-day chart.
        days: dict[str, int] = {}
        for day, tokens in conn.execute(
            f"{messages} SELECT date(m.time_created / 1000, 'unixepoch', 'localtime'),"
            f" COALESCE(SUM({_MESSAGE_TOKEN_SUM}), 0)"
            f" FROM src m WHERE {_ASSISTANT} AND m.time_created >= ?"
            f" GROUP BY 1",
            (week_ago,),
        ):
            if day:
                days[day] = days.get(day, 0) + tokens
        out.recent_days = [
            {
                "date": (datetime.now().astimezone() - timedelta(days=offset)).strftime(
                    "%Y-%m-%d"
                ),
                "messageCount": days.get(
                    (datetime.now().astimezone() - timedelta(days=offset)).strftime("%Y-%m-%d"),
                    0,
                ),
            }
            for offset in range(6, -1, -1)
        ]

        # Every date with any recorded usage, for the all-time "N days" line.
        # The panel unions these across synced machines, so they travel as
        # dates rather than a count.
        out.active_dates = [
            day
            for (day,) in conn.execute(
                f"{messages} SELECT DISTINCT date(m.time_created / 1000, 'unixepoch', 'localtime')"
                f" FROM src m WHERE {_ASSISTANT} ORDER BY 1"
            )
            if day
        ]

        # Per model, all time. The panel's own manifest calls this section the
        # "all-time model breakdown", so a date filter here would hide most of
        # the history from the one view built to show it. The 7-day window
        # already has its own section ("tokens by day").
        for model, i, o, r, cr, cw in conn.execute(
            f"{messages} SELECT m.model,"
            f" COALESCE(SUM(json_extract(m.data,'$.tokens.input')),0),"
            f" COALESCE(SUM(json_extract(m.data,'$.tokens.output')),0),"
            f" COALESCE(SUM(json_extract(m.data,'$.tokens.reasoning')),0),"
            f" COALESCE(SUM(json_extract(m.data,'$.tokens.cache.read')),0),"
            f" COALESCE(SUM(json_extract(m.data,'$.tokens.cache.write')),0)"
            f" FROM src m WHERE {_ASSISTANT} GROUP BY 1"
        ):
            if not model or "/" not in model:
                continue
            bucket = out.by_model.setdefault(model, ModelBucket())
            bucket.input_tokens += i
            bucket.output_tokens += o
            bucket.reasoning += r
            bucket.cache_read += cr
            bucket.cache_write += cw
        # Drop models that never actually spent anything: they are rows the
        # panel would draw as an empty bar.
        out.by_model = {
            model: bucket
            for model, bucket in out.by_model.items()
            if bucket.input_tokens or bucket.output_tokens
            or bucket.reasoning or bucket.cache_read or bucket.cache_write
        }

        # Today's split per model, same measure as the all-time one.
        for model, tokens in conn.execute(
            f"{messages} SELECT m.model,"
            f" COALESCE(SUM({_MESSAGE_TOKEN_SUM}), 0)"
            f" FROM src m WHERE {_ASSISTANT} AND m.time_created >= ? GROUP BY 1",
            (today,),
        ):
            if model and "/" in model:
                out.today_by_model[model] = tokens

        # Per agent. Both halves come from different tables on purpose:
        # `session` knows which runs were subagent runs and which are still
        # live, `message` knows what each agent actually spent and on what.
        for agent, sub, live, agent_sessions, cost in conn.execute(
            "SELECT COALESCE(agent,'(none)'),"
            " COALESCE(SUM(CASE WHEN parent_id IS NOT NULL THEN 1 ELSE 0 END),0),"
            f" COALESCE(SUM(CASE WHEN time_updated >= {active_cutoff} THEN 1 ELSE 0 END),0),"
            " COUNT(*), COALESCE(SUM(cost),0)"
            f" FROM {sessions} GROUP BY agent"
        ):
            entry = out.by_agent.get(agent) or AgentUsage(name=agent)
            entry.subagent_sessions += sub
            entry.active_sessions += live
            entry.sessions += agent_sessions
            entry.cost += cost or 0.0
            out.by_agent[agent] = entry

        # Prompts per agent, as its own query. A correlated subquery reading
        # session.agent would be the obvious way to write this, but that is not
        # legal next to a GROUP BY: the column is neither grouped nor
        # aggregated. Two queries merged here cost less than the workaround.
        for agent, prompts in conn.execute(
            f"{messages} SELECT COALESCE(json_extract(m.data,'$.agent'),'(none)'), COUNT(*)"
            f" FROM src m WHERE {_ASSISTANT} GROUP BY 1"
        ):
            entry = out.by_agent.get(agent) or AgentUsage(name=agent)
            entry.prompts += prompts or 0
            out.by_agent[agent] = entry

        # The model an agent is actually running, per agent and per model.
        # Not MAX(model) over a JSON blob: that compares strings and will
        # happily name a model the agent never used.
        for agent, model, tokens in conn.execute(
            f"{messages} SELECT COALESCE(json_extract(m.data,'$.agent'),'(none)'),"
            f" m.model,"
            f" COALESCE(SUM({_MESSAGE_TOKEN_SUM}), 0)"
            f" FROM src m WHERE {_ASSISTANT} GROUP BY 1, 2"
        ):
            if not model or "/" not in model:
                continue
            entry = out.by_agent.setdefault(agent, AgentUsage(name=agent))
            entry.models[model] = entry.models.get(model, 0) + tokens
            entry.tokens += tokens

        # Today, per agent: same measure as the all-time split, so the two
        # columns of the panel's table are comparable.
        for agent, tokens, prompts in conn.execute(
            f"{messages} SELECT COALESCE(json_extract(m.data,'$.agent'),'(none)'),"
            f" COALESCE(SUM({_MESSAGE_TOKEN_SUM}), 0), COUNT(*)"
            f" FROM src m WHERE {_ASSISTANT} AND m.time_created >= ? GROUP BY 1",
            (today,),
        ):
            entry = out.by_agent.setdefault(agent, AgentUsage(name=agent))
            entry.today_tokens += tokens
            entry.today_prompts += prompts

        # The model an agent ran *last*, not the one it spent the most on. An
        # agent is not pinned to a model, so the heaviest all-time is often a
        # model the user abandoned months ago. Ordering the whole source by
        # timestamp and keeping the first row per agent needs no window
        # function, so it works on every SQLite opencode ships against.
        last_used: dict[str, str] = {}
        for agent, model in conn.execute(
            f"{messages} SELECT COALESCE(json_extract(m.data,'$.agent'),'(none)'),"
            f" m.model FROM src m"
            f" WHERE {_ASSISTANT} AND m.model IS NOT NULL"
            f" AND instr(m.model,'/') > 0 AND m.time_created IS NOT NULL"
            f" ORDER BY m.time_created DESC"
        ):
            last_used.setdefault(agent, model)

        for entry in out.by_agent.values():
            if entry.models:
                # `models` stays heaviest first: the CLI's "+N more" and the
                # panel's breakdown are ordered by volume. The single `model`
                # is the last one used, falling back to the heaviest when no
                # timestamp resolves.
                ordered = sorted(entry.models.items(), key=lambda kv: -kv[1])
                last = last_used.get(entry.name)
                entry.model = last if last in entry.models else ordered[0][0]
                entry.models = dict(ordered)
            entry.variant = _model_variant(entry.model)

    @staticmethod
    def _count_prompts(
        conn: sqlite3.Connection, source: str, since: int | None = None
    ) -> int:
        """Assistant messages are opencode's unit of work. Reading the JSON
        payload is the one query that scales with history; the caller caches
        its result by database mtime. `source` is the normalized message CTE,
        so the count means the same on both schemas."""
        sql = f"{source} SELECT COUNT(*) FROM src WHERE type = 'assistant'"
        params: tuple = ()
        if since is not None:
            sql += " AND time_created >= ?"
            params = (since,)
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        except sqlite3.Error:
            return 0
