# Connecting the Gemini app to memory-manager

**Not possible (reliably) today.** This page documents why, not how.

Checked against the vendor documentation and a secondary developer report, retrieved
2026-10-11 — no memory-manager version was ever involved, since no connection was ever
attempted.

## Tested version

Not applicable — never tested, and not testable from here (see "Why not").

## Why not

The consumer Gemini app (`gemini.google.com`) lets users link custom apps by MCP server URL under
Settings → Connected Apps → Custom apps, mainly for the Gemini Spark agent. Three restrictions
make it unusable here even where the feature itself works:

- **US only.**
- **18+, personal Google Account only — no work or school accounts.** memory-manager has no org
  with US-only, personal-account users to test or roll this out to.
- **English language setting required.**

On top of that, a developer forum report dated 2026-10-06 describes a token exchange that never
completes: Gemini registers a client via dynamic client registration, negotiates PKCE with
`code_challenge_method=S256`, and gets an RFC 9728 protected-resource response back — the same
shape memory-manager's own authorization server expects — but Gemini never calls the token
endpoint at all. The sign-in redirect ends on a Google page reading "Cannot Complete Request",
and the only path offered from there is "Report a problem", not a retry. That report is
`[secondary]` (one forum thread, not a vendor statement), but it describes exactly the auth shape
this server requires, which makes it a real risk rather than an unrelated edge case.

Full detail and sources: [`../research/clients/gemini-app.md`](../research/clients/gemini-app.md).

## Setup

Not applicable — no setup is documented here, because the restrictions above mean there is no
account this project can use to perform or verify it.

## Auth variants

Not applicable. For reference, Google documents dynamic client registration as the default, plus
an "Advanced features" path where the user enters a client's own credentials by hand; neither has
been tested against memory-manager, and the forum report above suggests the default path may not
complete at all.

## Instructions file

Not applicable — `[unverified]` either way, and untestable without a usable account.

## Known limits

- Restricted to US, 18+, personal Google accounts, English language setting — excludes every
  account shape this project can test with or roll out to an organisation.
- Write actions require the user to confirm each call (documented, not independently verified).
- A reported OAuth failure matches memory-manager's own auth requirements (DCR, PKCE S256,
  RFC 9728 discovery) closely enough that even a US personal account might not be able to
  complete sign-in; unconfirmed either way.

## Org rollout

Not applicable — the consumer app has no organisation-level control at all; work and school
accounts are excluded from the feature entirely.

## Troubleshooting

Not applicable — no connection has ever been established to troubleshoot.

## Revisit triggers

Re-open this page if any of the following happens:

- Google extends custom apps to work/school accounts, or outside the US.
- The reported token-exchange failure ([`../research/clients/gemini-app.md`](../research/clients/gemini-app.md) [G7]) is fixed or confirmed resolved.
- Someone with a qualifying US personal account runs the manual checklist
  ([`../release/client-checklist.md`](../release/client-checklist.md)) end to end.
