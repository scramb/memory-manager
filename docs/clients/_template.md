# Connecting `<client>` to memory-manager

One page per client follows this template. Replace `<client>` with the client's name and fill
every section; drop a section only when the client genuinely has nothing to say there (state
that explicitly, do not just delete it).

Checked against `<client version>` on `<YYYY-MM-DD>`.

## Tested version

The exact client build (and memory-manager version) the instructions below were verified
against, and the date of that verification. Carry over the "Verified" line from the source guide
or research note rather than re-deriving it.

## Setup

### Global

How to add the server for every project/session of this user (the default, where the client
supports it).

### Project

How to add the server scoped to one project, and what that changes (shared config file,
collaborators, credentials kept out of it).

## Auth variants

Every auth method this client supports against our server (OAuth via CIMD, OAuth via DCR, a
pre-registered OAuth client, a static token/header), with the exact flag or UI step for each, and
which one is the default.

## Instructions file

Whether the client loads the server's MCP `instructions` automatically, or needs a separate
instructions/rules file instead — and where that file lives.

## Known limits

Hard limits that change what works: result size, timeout, truncation of `instructions`/tool
descriptions, tool-count caps, anything the client silently drops or rewrites.

## Org rollout

How an organisation deploys this for many users: shared config, admin policy, what credentials
(if any) end up in a file collaborators see.

## Troubleshooting

Symptom/cause/fix table for the failures users actually hit, and how `doctor --client <client>`
(once it exists — see `docs/features/F-02-client-integrations.md` for its status) helps diagnose
them.
