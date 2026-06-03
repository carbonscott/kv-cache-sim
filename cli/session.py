"""The shared command session: parse a command line, drive the engine, return output.

This is the core both front-ends use. A turn is *accumulated* from one or more
`user` / `tool` / `assistant` commands and committed as a single Turn by `send` (or a
blank line) -- mirroring how a real Claude Code request bundles user input, several
tool results, and one generation into one request.

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
    ClearToolResults,
    Compact,
    Rewind,
    SwitchEffort,
    SwitchModel,
    Turn,
    Upgrade,
)
from sim.state import CacheState
from sim.tokenizer import count_tokens, is_approximate

from . import ledger

HELP = """commands:
  user <n | text...>      add user-input tokens to the pending turn
  tool <name> <n>         add a fake tool-result of n tokens to the pending turn
  assistant <n | text...> add assistant-output tokens to the pending turn
  send                    commit the pending turn (a blank line does the same)
  advance <dur>           jump the clock (e.g. 90s, 6m, 1h, or bare seconds)
  ttl <5m | 1h>           switch cache TTL (no invalidation; changes the write rate)
  rewind <to_tokens>      truncate back to an earlier prefix length (re-hits in TTL)
  compact <summary_tokens> replace the conversation layer with a summary (keeps system)
  clear-tools <n>         clear n tokens of old tool results (invalidates downstream)
  model <name>            switch model (invalidates the cache)
  effort <level>          switch effort level (invalidates the cache)
  upgrade                 simulate a CC upgrade (invalidates the cache, even in TTL)
  status                  show current session state and the pending turn
  help                    show this help
  quit | exit             leave the session"""

# The canonical list of top-level command verbs, used by the REPL's Tab
# completer. Kept in sync with the if/elif chain in Session.handle().
COMMAND_NAMES = (
    "user", "tool", "assistant", "send", "advance", "ttl", "rewind", "compact",
    "clear-tools", "model", "effort", "upgrade", "status", "help",
    "quit", "exit",
)

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


class Session:
    """Holds engine state, the running cost, and the pending (uncommitted) turn."""

    def __init__(self, state: CacheState, config: Config):
        self.state = state
        self.config = config
        self.encoding = config.tokenizer
        self.running_total = 0.0
        self.turn_index = 0
        self.pending_input = 0
        self.pending_output = 0
        self.pending_input_blocks = 0
        self.pending_output_blocks = 0
        self.done = False

    # -- pending-turn helpers ------------------------------------------------

    def _has_pending(self) -> bool:
        return self.pending_input > 0 or self.pending_output > 0

    def _commit_turn(self) -> list[str]:
        if not self._has_pending():
            return ["(nothing to send: the pending turn is empty)"]
        event = Turn(
            input_tokens=self.pending_input,
            output_tokens=self.pending_output,
            input_blocks=self.pending_input_blocks,
            output_blocks=self.pending_output_blocks,
        )
        self.pending_input = 0
        self.pending_output = 0
        self.pending_input_blocks = 0
        self.pending_output_blocks = 0
        return self._apply(event)

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
            return self._commit_turn()  # blank line commits the pending turn
        if stripped.startswith("#"):
            return []  # comment line: ignored by both front-ends

        parts = stripped.split()
        command, args = parts[0].lower(), parts[1:]

        if command in ("quit", "exit"):
            self.done = True
            return ["bye."]
        if command == "help":
            return [HELP]
        if command == "status":
            return self._status()
        if command == "send":
            return self._commit_turn()
        if command == "user":
            return self._user(args)
        if command == "tool":
            return self._tool(args)
        if command == "assistant":
            return self._assistant(args)
        if command == "advance":
            return self._advance(args)
        if command == "ttl":
            return self._set_ttl(args)
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
        return [f"+{added} input tok (pending turn: in={self.pending_input}, "
                f"out={self.pending_output})"]

    def _tool(self, args: list[str]) -> list[str]:
        if len(args) < 2 or not re.fullmatch(r"\d+", args[-1]):
            return ["usage: tool <name> <n>   (n = fake tool-result token count)"]
        name = " ".join(args[:-1])
        added = int(args[-1])
        self.pending_input += added
        self.pending_input_blocks += 2  # a tool round-trip = tool_use + tool_result blocks
        return [f"+{added} tool-result tok from {name!r} "
                f"(pending turn: in={self.pending_input}, out={self.pending_output})"]

    def _assistant(self, args: list[str]) -> list[str]:
        if not args:
            return ["usage: assistant <n | text...>"]
        added = _tokens_or_text(" ".join(args), self.encoding)
        self.pending_output += added
        self.pending_output_blocks += 1  # an assistant generation is one content block
        return [f"+{added} output tok (pending turn: in={self.pending_input}, "
                f"out={self.pending_output})"]

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

    def _status(self) -> list[str]:
        lines = [ledger.status_line(self.state, self.running_total)]
        if self._has_pending():
            lines.append(
                f"pending turn: in={self.pending_input}, out={self.pending_output} "
                f"(use 'send' or a blank line to commit)"
            )
        if is_approximate(self.encoding):
            lines.append(f"note: tokenizer {self.encoding!r} unavailable; "
                         f"using a chars/4 estimate")
        return lines
