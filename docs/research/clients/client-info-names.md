# Client: `clientInfo.name` values (Claude Code, claude.ai)

Retrieved: 2026-10-10 · Tested version: Claude Code 2.1.280 (binary installed on this host); claude.ai: not reachable from the
research environment

This note exists for #131 (`compat/select.py`'s override → `clientInfo.name` → `default` resolver, ADR-0010). It only asks one
question per client: which string does the client send as the MCP `initialize` request's `params.clientInfo.name`, so that
`compat/select.py` can map it to a registered `Profile` name (ADR-0010: unmapped names must stay unmapped, never guessed).

## Claude Code — sourced: `claude-code`

**Primary source, read directly from the shipped binary.** The locally installed Claude Code CLI
(`/home/scramb/.local/bin/claude` → `/home/scramb/.local/share/claude/versions/2.1.280`, an ELF binary bundling a minified JS
runtime) constructs its MCP `Client` with a literal `clientInfo` object before connecting to any configured MCP server
(stdio or HTTP; the surrounding code branches on `n.type==="stdio"` vs. other transports but feeds the same object into the
client constructor either way):

```
new K5n({name:"claude-code",title:"Claude Code",version:{...}.VERSION??"unknown",
         description:"Anthropic's agentic coding tool",websiteUrl:c1e},
        {capabilities:...,jsonSchemaValidator:...,versionNegotiation:v,...})
```

(`K5n` and, at a second call site for the other transport branch, `ht` are both minified identifiers for the MCP SDK's
`Client` class; the `{...}.VERSION` object is the CLI's own build-info record, resolving to the literal string `"2.1.280"`.)

Extracted verbatim from the binary with `strings`/a byte-level regex scan, retrieved 2026-10-10:

```
name:"claude-code",title:"Claude Code",version:{ISSUES_EXPLAINER:"report the issue at
https://github.com/anthropics/claude-code/issues",PACKAGE_URL:"@anthropic-ai/claude-code",
README_URL:"https://code.claude.com/docs/en/overview",VERSION:"2.1.280", ...}.VERSION??"unknown"
```

So Claude Code sends `clientInfo.name = "claude-code"` (lower-case, hyphenated — matching the npm package
`@anthropic-ai/claude-code` and the CLI binary name, not the display title `"Claude Code"`). This matches the existing
`docs/clients/README.md:45` / `docs/research/mcp-auth-and-connectors.md` §5 identification of this client and is consistent
with the registered profile name `claude-code` in `compat/profiles.py`.

Two other literal `clientInfo.name` values exist in the same binary, for unrelated internal features, not the user-facing
MCP-server connection this project cares about: `"remote-tools-bridge"` (an internal bridge protocol, `protocolVersion:
"2024-11-05"`) and `"claude-cli-design-tool"` (an internal design-tool integration, `protocolVersion: "2025-03-26"`). Neither
applies to a `claude mcp add` connection to a server like memory-manager.

## claude.ai — not sourced, left unmapped

claude.ai is a server-side web application; there is no local binary to read the way the Claude Code CLI permits above, and
the live site (`https://claude.ai/`) returns a Cloudflare bot-challenge page to this environment's non-browser `curl`
requests, so its bundled client JS could not be fetched and inspected. No vendor documentation reviewed for
`docs/research/mcp-auth-and-connectors.md` (§4, retrieved 2026-10-06) states the literal `clientInfo.name` string either.

Per ADR-0010 and the guideline "verify, don't recall": an unsourced name stays unmapped. `compat/select.py` therefore maps
only `"claude-code"` → the `claude-code` profile; any other `clientInfo.name`, including whatever claude.ai actually sends,
falls through to `DEFAULT_PROFILE`. A client identified later with a sourced name can be added to the mapping without
changing this resolution order.

## Sources

- [CC1] Local installation, Claude Code CLI binary, `/home/scramb/.local/share/claude/versions/2.1.280` (`claude --version`
  reports `2.1.280`; `BUILD_TIME:"2026-09-21T20:40:17Z"`, `GIT_SHA:"80abbfe7d7232280011ff01a21ae3338f4c6e372"` embedded in the
  same literal), read with `strings`/a Python byte-regex scan, retrieved 2026-10-10.
- [CC2] `curl -A "Mozilla/5.0" https://claude.ai/`, retrieved 2026-10-10: returns a Cloudflare "Just a moment..." challenge
  page (HTTP 200, `noindex,nofollow`), no application bundle served to this client.
