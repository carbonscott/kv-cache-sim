"""The shared command session: parse a command line, drive the engine, return output.

This is the core both front-ends use. The API call is the atomic, billed unit, mirroring
a real agent loop's `while True: resp = call_llm(...)`. `user` / `tool` commands are free
local appends that accumulate pending input (tokens + content blocks); `call <out> [tu=N]`
is the only billed event -- it issues one API request that reads the accumulated input,
emits the generation's output, and resets the pending input.

Session has no I/O of its own: handle() returns a list of output lines for the caller
(REPL or batch runner) to print.
"""

from __future__ import annotations

import re
from dataclasses import replace

from sim.config import Config
from sim.engine import apply_event
from sim.events import (
    Advance,
    Call,
    ClearToolResults,
    Compact,
    Rewind,
    SwitchEffort,
    SwitchModel,
    Upgrade,
)
from sim.state import CacheState
from sim.tokenizer import count_tokens, is_approximate

from . import ledger

HELP = """commands:
  user <n | text...>      append user-input tokens to the pending call (free; +1 block)
  tool <name> <n>         append a fake tool-result of n tokens (free; +1 block)
  call <out> [tu=N]       issue one API request: bill the accumulated input, emit <out>
                          output tokens in max(1, N) blocks, then reset the pending input
  advance <dur>           jump the clock (e.g. 90s, 6m, 1h, or bare seconds)
  ttl <5m | 1h>           switch cache TTL (no invalidation; changes the write rate)
  system <n>              set the protected prefix length (cold session only)
  rewind <to_tokens>      truncate back to an earlier prefix length (re-hits in TTL)
  compact <summary_tokens> replace the conversation layer with a summary (keeps system)
  clear-tools <n>         clear n tokens of old tool results (invalidates downstream)
  model <name>            switch model (invalidates the cache)
  effort <level>          switch effort level (invalidates the cache)
  upgrade                 simulate a CC upgrade (invalidates the cache, even in TTL)
  status                  show current session state and the pending input
  reset                   wipe everything back to a fresh cold session
  save <file>             write this session's commands to a replayable file
  load <file>             replace this session by replaying a saved file
  help                    show this help
  quit | exit             leave the session"""

# The canonical list of top-level command verbs, used by the REPL's Tab
# completer. Kept in sync with the if/elif chain in Session.handle().
# `save`/`load` are handled by the REPL front-end (file I/O lives there); the
# batch runner doesn't support them.
COMMAND_NAMES = (
    "user", "tool", "call", "advance", "ttl", "system", "rewind", "compact",
    "clear-tools", "model", "effort", "upgrade", "status", "reset", "save",
    "load", "help", "quit", "exit",
)

# State-changing verbs that get recorded into Session.history for save/replay.
# `user`/`tool` accumulate the pending call; `call` bills it -- all three are
# recorded so a save round-trips. Unknown verbs and read-only verbs (status/help)
# are not recorded.
RECORDABLE = frozenset({
    "user", "tool", "call", "advance", "ttl", "system", "rewind",
    "compact", "clear-tools", "model", "effort", "upgrade", "reset",
})

_DURATION = re.compile(r"^(\d+)\s*([smh]?)$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> int:
    """Parse 90s / 6m / 1h / bare-seconds into seconds. Raises ValueError on junk."""
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError(f"bad duration: {text!r} (try 90s, 6m, 1h, or a number)")
    value, unit = match.groups()
    return int(value) * _UNIT_SECONDS[unit]


def _tokens_or_text(arg: str, encoding: str) -> int:
    """Interpret an argument as a token count if it's a bare integer, else tokenize
    it as text."""
    stripped = arg.strip()
    if re.fullmatch(r"\d+", stripped):
        return int(stripped)
    return count_tokens(arg, encoding)


def default_state(config: Config) -> CacheState:
    """A fresh, cold session on the first model in the config at 5-minute TTL."""
    first_model = next(iter(config.models))
    return CacheState(
        model=first_model,
        effort="high",
        ttl_seconds=config.ttl_seconds["5m"],
        last_used=0.0,
        now=0.0,
        prefix_tokens=0,
        cached_tokens=0,
    )


class Session:
    """Holds engine state, the running cost, and the pending (un-billed) input."""

    def __init__(self, state: CacheState, config: Config):
        self.state = state
        self.config = config
        self.encoding = config.tokenizer
        self.running_total = 0.0
        self.turn_index = 0
        self.pending_input = 0
        self.pending_input_blocks = 0
        self.done = False
        self.just_reset = False  # set by `reset`; the REPL checks it to reprint its banner
        self.history: list[str] = []  # state-changing commands, for save/load replay

    # -- engine application --------------------------------------------------

    def _apply(self, event) -> list[str]:
        """Apply an event to the engine and format its ledger row + notes."""
        self.state, cost = apply_event(self.state, event, self.config)
        self.running_total += cost.total_cost
        self.turn_index += 1
        lines = [
            ledger.row(
                self.turn_index,
                ledger.describe_event(event),
                cost,
                self.running_total,
            )
        ]
        lines.extend(ledger.note_line(n) for n in cost.notes)
        return lines

    # -- command handling ----------------------------------------------------

    def handle(self, line: str) -> list[str]:
        """Process one command line, returning output lines to print."""
        stripped = line.strip()
        if stripped == "":
            return []  # blank line: a no-op (the API call is the only billed event)
        if stripped.startswith("#"):
            return []  # comment line: ignored by both front-ends

        parts = stripped.split()
        command, args = parts[0].lower(), parts[1:]

        if command in RECORDABLE:
            self.history.append(stripped)

        if command in ("quit", "exit"):
            self.done = True
            return ["bye."]
        if command == "help":
            return [HELP]
        if command == "status":
            return self._status()
        if command == "reset":
            return self._reset()
        if command == "user":
            return self._user(args)
        if command == "tool":
            return self._tool(args)
        if command == "call":
            return self._call(args)
        if command == "advance":
            return self._advance(args)
        if command == "ttl":
            return self._set_ttl(args)
        if command == "system":
            return self._system(args)
        if command == "rewind":
            return self._rewind(args)
        if command == "compact":
            return self._compact(args)
        if command == "clear-tools":
            return self._clear_tools(args)
        if command == "model":
            return self._switch_model(args)
        if command == "effort":
            return self._switch_effort(args)
        if command == "upgrade":
            return self._apply(Upgrade())
        return [f"unknown command: {command!r} (try 'help')"]

    def _user(self, args: list[str]) -> list[str]:
        if not args:
            return ["usage: user <n | text...>"]
        added = _tokens_or_text(" ".join(args), self.encoding)
        self.pending_input += added
        self.pending_input_blocks += 1  # a user message materializes one content block
        return [f"+{added} input tok (pending input: {self.pending_input} tok, "
                f"{self.pending_input_blocks} blocks)"]

    def _tool(self, args: list[str]) -> list[str]:
        if len(args) < 2 or not re.fullmatch(r"\d+", args[-1]):
            return ["usage: tool <name> <n>   (n = fake tool-result token count)"]
        name = " ".join(args[:-1])
        added = int(args[-1])
        self.pending_input += added
        # Only the tool_result is input; the tool_use block belongs to the requesting
        # call's output, so a tool result is one input block.
        self.pending_input_blocks += 1
        return [f"+{added} tool-result tok from {name!r} "
                f"(pending input: {self.pending_input} tok, "
                f"{self.pending_input_blocks} blocks)"]

    def _call(self, args: list[str]) -> list[str]:
        """Issue one API request: bill the accumulated input, emit <out> output tokens
        in max(1, tu) blocks, then reset the pending input. This is the only billed
        event -- it mirrors one `resp = call_llm(...)` in an agent loop."""
        if not args or not re.fullmatch(r"\d+", args[0]):
            return ["usage: call <out> [tu=N]   (out = output tokens, N = tool_use blocks)"]
        out = int(args[0])
        tu = 0
        for extra in args[1:]:
            match = re.fullmatch(r"tu=(\d+)", extra)
            if not match:
                return ["usage: call <out> [tu=N]   (out = output tokens, N = tool_use blocks)"]
            tu = int(match.group(1))
        event = Call(
            input_tokens=self.pending_input,
            output_tokens=out,
            input_blocks=self.pending_input_blocks,
            output_blocks=max(1, tu),  # tu real tool_use blocks, or 1 text block when tu=0
        )
        self.pending_input = 0
        self.pending_input_blocks = 0
        return self._apply(event)

    def _advance(self, args: list[str]) -> list[str]:
        if not args:
            return ["usage: advance <dur>   (e.g. 90s, 6m, 1h)"]
        try:
            seconds = parse_duration(args[0])
        except ValueError as e:
            return [str(e)]
        return self._apply(Advance(seconds=seconds))

    def _set_ttl(self, args: list[str]) -> list[str]:
        """Switch the cache TTL (e.g. 5m or 1h). TTL is auth-/time-driven, not part of
        the cache key, so changing it invalidates nothing -- it only changes when the
        cache expires and which write multiplier future turns pay."""
        if not args or args[0] not in self.config.ttl_seconds:
            known = ", ".join(self.config.ttl_seconds)
            return [f"usage: ttl <{known}>"]
        seconds = self.config.ttl_seconds[args[0]]
        self.state = replace(self.state, ttl_seconds=seconds)
        rate = self.config.write_multiplier(seconds)
        return [f"ttl set to {args[0]} ({seconds}s); cache kept (write rate now {rate}x)"]

    def _system(self, args: list[str]) -> list[str]:
        """Set the protected leading prefix (system + tool defs + project context).
        A pure cold marker: it sets system_tokens only -- it does NOT seed prefix or
        cached tokens. The first `call`'s input naturally carries the protected region,
        so the cold first call writes it once at write-rate, identical to system=0.
        Mirrors _set_ttl: a bare replace(), no _apply(), no ledger row, no event.

        Cold guard: refuse unless prefix_tokens == 0. Setting system mid-session can
        drive the conversation layer negative and silently no-op compact/clear-tools,
        so the contract requires setting it before the conversation grows."""
        if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
            return ["usage: system <n>   (protected prefix tokens)"]
        if self.state.prefix_tokens != 0:
            return ["system must be set on a cold session "
                    "(prefix_tokens == 0); reset first"]
        n = int(args[0])
        self.state = replace(self.state, system_tokens=n)
        return [f"protected prefix set to {n} tok"]

    def _rewind(self, args: list[str]) -> list[str]:
        if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
            return ["usage: rewind <to_tokens>   (absolute prefix length to keep)"]
        return self._apply(Rewind(to_tokens=int(args[0])))

    def _compact(self, args: list[str]) -> list[str]:
        if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
            return ["usage: compact <summary_tokens>   (size of the replacement summary)"]
        return self._apply(Compact(summary_tokens=int(args[0])))

    def _clear_tools(self, args: list[str]) -> list[str]:
        if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
            return ["usage: clear-tools <n>   (tokens of old tool results to clear)"]
        return self._apply(ClearToolResults(freed_tokens=int(args[0])))

    def _switch_model(self, args: list[str]) -> list[str]:
        if not args:
            return ["usage: model <name>"]
        name = args[0]
        if name not in self.config.models:
            known = ", ".join(sorted(self.config.models))
            return [f"unknown model: {name!r} (known: {known})"]
        return self._apply(SwitchModel(model=name))

    def _switch_effort(self, args: list[str]) -> list[str]:
        if not args:
            return ["usage: effort <level>"]
        return self._apply(SwitchEffort(effort=args[0]))

    def _reset_session(self, clear_history: bool) -> None:
        """Wipe engine state, cost, and the pending input back to a fresh cold session.
        Shared by `reset` (keeps history, so replay reproduces the reset) and `load`
        (clears history before replaying a saved file). Keeps config/encoding."""
        self.state = default_state(self.config)
        self.running_total = 0.0
        self.turn_index = 0
        self.pending_input = 0
        self.pending_input_blocks = 0
        if clear_history:
            self.history = []

    def _reset(self) -> list[str]:
        """Wipe the run back to a fresh cold session, as if the app were relaunched.
        Keeps config/encoding (and stays running); the REPL reprints its banner via
        the just_reset flag. History is kept -- `handle()` records the `reset` command
        like any other, so a later replay reproduces the reset."""
        self._reset_session(clear_history=False)
        self.just_reset = True
        return ["session reset to a fresh cold session."]

    def load_commands(self, lines: list[str]) -> list[str]:
        """Replace this session by replaying a saved command file. Resets to a fresh
        cold session (clearing history), then feeds each line through handle() -- the
        same pure engine path as batch mode -- so cost, turn index, and the pending
        input are all re-derived deterministically. Because handle() re-records the
        replayed commands, self.history ends equal to the file's recordable lines, so
        a subsequent save round-trips. Returns the accumulated replay output."""
        self._reset_session(clear_history=True)
        output: list[str] = []
        for line in lines:
            output.extend(self.handle(line))
        return output

    def _status(self) -> list[str]:
        lines = [ledger.status_line(self.state, self.running_total)]
        if self.pending_input > 0:
            lines.append(
                f"pending input: {self.pending_input} tok, "
                f"{self.pending_input_blocks} blocks (bill it with 'call <out>')"
            )
        if is_approximate(self.encoding):
            lines.append(f"note: tokenizer {self.encoding!r} unavailable; "
                         f"using a chars/4 estimate")
        return lines
