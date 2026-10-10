# SPDX-License-Identifier: AGPL-3.0-only
"""The known client compatibility profiles, as data (ADR-0010).

A `Profile` carries two things an incoming request needs once #131 picks one: how the
usage rules reach the client (`delivery_mode`) and the client's documented limits, which
the schema linter (#133) will check the tool contract against. Nothing here decides
*which* profile a request gets, and nothing here changes a tool's behaviour - that would
make a profile a code path, which ADR-0010 rules out.

No note under `docs/research/clients/` exists yet for claude.ai or Claude Code (unlike the
other F-02 clients, which each get one); their limits here are sourced from
`docs/research/mcp-auth-and-connectors.md` (retrieved 2026-10-06) and from
`docs/PLAN.md`'s "Protocol targets" section instead. Every limit below, set or left
`None`, carries that source in a comment. `None` means "not documented", never
"unlimited" - a linter that reads a `None` as "no check needed" is correct; one that reads
it as "any size permitted" would be wrong.

Units live in the field names on purpose (`_chars` vs. `_tokens`): characters and tokens
are not interchangeable, and this module never converts between them.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

__all__ = [
    "DEFAULT_PROFILE",
    "DeliveryMode",
    "Profile",
    "UnknownProfile",
    "get_profile",
    "profile_names",
]

#: How the usage rules (today's `INSTRUCTIONS`/`GUIDE`, `mcp/instructions.py`) reach a
#: client: the full `instructions` text at `initialize` ("full"), the rules folded into
#: tool descriptions for a client that drops `instructions` ("descriptions", ADR-0010's
#: "Additive changes"), or a short form for a client that truncates both ("short"). #131
#: picks the mode per request; this module only names the three (pattern `quotas.py`'s
#: `QuotaScope`).
DeliveryMode = Literal["full", "descriptions", "short"]


class UnknownProfile(ValueError):
    """`get_profile` was asked for a name that is not in the registry.

    Raised rather than silently falling back to `default` (ADR-0010: "Unknown profile
    names are rejected ... never silently mapped") - same contract as `vault.repo.
    author_for`'s `ValueError` for an unknown client.
    """


@dataclass(frozen=True)
class Profile:
    """One client's delivery mode and documented limits.

    `name` is the registry key the profile was looked up by (`get_profile` enforces
    `profile.name == name`), so a `Profile` carries its own identity and can be passed
    around without the key that found it.
    """

    name: str
    delivery_mode: DeliveryMode

    #: Server `instructions` (MCP `initialize` result), in characters. `None` = not
    #: documented for this client.
    max_instructions_chars: int | None

    #: A single tool's `description`, in characters. `None` = not documented.
    max_tool_description_chars: int | None

    #: A single tool call result, in characters. `None` = not documented, or the client
    #: budgets results in tokens instead (see `result_budget_tokens`).
    result_budget_chars: int | None

    #: A single tool call result, in tokens. `None` = not documented, or the client
    #: budgets results in characters instead (see `result_budget_chars`).
    result_budget_tokens: int | None

    #: A tool's name, including any server-id prefix the client adds, in characters.
    #: `None` = not documented.
    max_tool_name_chars: int | None

    #: JSON Schema keywords this client's tool-schema parser rejects outright (e.g. a
    #: Gemini-style OpenAPI subset refusing `$ref`). Empty for every profile here - none
    #: of the three documents such a restriction.
    disallowed_schema_keywords: frozenset[str]

    #: The maximum number of tools this client will accept from one server. `None` = not
    #: documented.
    max_tools: int | None


_DEFAULT = Profile(
    name="default",
    # Today's behaviour for every client without a more specific profile
    # (mcp/server.py:780 always sends the full `instructions` text at `initialize`).
    delivery_mode="full",
    # docs/PLAN.md:89 "Protocol targets": "Server instructions ≤ 2,048 characters
    # (Claude Code truncates)" - kept as the conservative default baseline.
    max_instructions_chars=2048,
    # Not documented for an unspecified client; no blanket tool-description limit is
    # claimed in docs/PLAN.md.
    max_tool_description_chars=None,
    # docs/PLAN.md:89 "Protocol targets": "tool results well under 150,000 characters
    # (claude.ai cap)" - kept as the conservative default baseline.
    result_budget_chars=150_000,
    # Not documented for an unspecified client; the default baseline budgets in
    # characters (result_budget_chars above), not tokens.
    result_budget_tokens=None,
    # Not documented for an unspecified client.
    max_tool_name_chars=None,
    # No client reviewed so far documents a disallowed-keyword subset.
    disallowed_schema_keywords=frozenset(),
    # Not documented for an unspecified client.
    max_tools=None,
)

_CLAUDE_AI = Profile(
    name="claude-ai",
    # docs/features/F-02-client-integrations.md:177, support matrix: server
    # `instructions` support is "yes" for claude.ai, with no documented truncation - see
    # max_instructions_chars below.
    delivery_mode="full",
    # docs/research/mcp-auth-and-connectors.md §4 (l.181 is Claude Code's truncation;
    # claude.ai's own "Limits:" bullets, l.155-158, document no instructions-length cap)
    # [C1], retrieved 2026-10-06: not documented for claude.ai.
    max_instructions_chars=None,
    # Not documented: the same §4 "Limits:" bullets cover only tool-result size and call
    # timeout, no tool-description cap.
    max_tool_description_chars=None,
    # docs/research/mcp-auth-and-connectors.md §4 l.156 [C1], retrieved 2026-10-06:
    # "claude.ai and Desktop: max tool result ~150,000 characters".
    result_budget_chars=150_000,
    # claude.ai budgets results in characters (result_budget_chars above), not tokens;
    # the token-based MAX_MCP_OUTPUT_TOKENS limit (§4 l.157) is Claude Code's.
    result_budget_tokens=None,
    # Not documented: §5 l.182 documents a tool-name prefix for prompts
    # (`/mcp__server__prompt`) only, not a length cap on tool names themselves.
    max_tool_name_chars=None,
    # No disallowed-keyword subset is documented for claude.ai.
    disallowed_schema_keywords=frozenset(),
    # docs/research/mcp-auth-and-connectors.md §4 l.158 [C1], retrieved 2026-10-06: "No
    # tool-count limit is documented in the pages reviewed."
    max_tools=None,
)

_CLAUDE_CODE = Profile(
    name="claude-code",
    # docs/features/F-02-client-integrations.md:178, support matrix: server
    # `instructions` support is "yes (≤ 2,048 chars)" for Claude Code.
    delivery_mode="full",
    # docs/research/mcp-auth-and-connectors.md §5 l.181 [K1], retrieved 2026-10-06:
    # "Tool descriptions and instructions are truncated at 2,048 characters each."
    max_instructions_chars=2048,
    # docs/research/mcp-auth-and-connectors.md §5 l.181 [K1], retrieved 2026-10-06: same
    # sentence, "each" covers tool descriptions too.
    max_tool_description_chars=2048,
    # Claude Code budgets results in tokens (result_budget_tokens below), not characters;
    # §4 l.156's character budget is claude.ai's.
    result_budget_chars=None,
    # docs/research/mcp-auth-and-connectors.md §4 l.157, retrieved 2026-10-06: "Claude
    # Code: 25,000 tokens (MAX_MCP_OUTPUT_TOKENS)" - the SDK's documented default; the
    # user can raise it locally, which this profile does not attempt to track.
    result_budget_tokens=25_000,
    # Not documented: §5 l.182 documents a tool-name prefix for prompts
    # (`/mcp__server__prompt`) only, not a length cap on tool names themselves.
    max_tool_name_chars=None,
    # No disallowed-keyword subset is documented for Claude Code.
    disallowed_schema_keywords=frozenset(),
    # docs/research/mcp-auth-and-connectors.md §4 l.158 [C1], retrieved 2026-10-06: "No
    # tool-count limit is documented in the pages reviewed." - Claude Code shares
    # claude.ai's connectors (§4 l.160) and no Claude Code-specific count is documented
    # either.
    max_tools=None,
)

#: The name `get_profile` falls back to once #131 decides a request's `clientInfo`
#: matched nothing and no override was given.
DEFAULT_PROFILE = "default"

_REGISTRY: MappingProxyType[str, Profile] = MappingProxyType(
    {profile.name: profile for profile in (_DEFAULT, _CLAUDE_AI, _CLAUDE_CODE)}
)


def get_profile(name: str) -> Profile:
    """Look up a profile by its exact, case-sensitive name.

    Raises `UnknownProfile` - naming `name` and every valid name - if `name` is not in
    the registry, including the wrong case (`"Claude-AI"`) or the empty string. Never
    falls back to `default` itself; a caller that wants that fallback (#131) asks for
    `DEFAULT_PROFILE` explicitly.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        valid = ", ".join(profile_names())
        raise UnknownProfile(f"unknown profile {name!r}, expected one of ({valid})") from None


def profile_names() -> tuple[str, ...]:
    """Every valid profile name, sorted."""
    return tuple(sorted(_REGISTRY))
