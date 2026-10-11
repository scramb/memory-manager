# Client: Gemini app and Gemini Enterprise

Retrieved: 2026-10-11 · Tested version: none (desk research; latest: hosted services, no version)

**Source quality note:** the 2026-10-07 pass found `support.google.com`, `docs.cloud.google.com`
and `discuss.ai.google.dev` blocked by the research environment's egress proxy and worked from
search-engine snippets only. On 2026-10-11 all three were reachable directly; [GE] (the Gemini
Enterprise custom-MCP setup page), [GB] (its Business-edition counterpart), [G1] (the consumer
Connected Apps help page) and [G7] (the forum thread) were read in full and are no longer
snippet-only. `[secondary]` still marks claims backed only by third-party sources (forum posts,
blog posts) rather than a vendor page — a full read of a secondary source stays `[secondary]`,
it just stops being `[unverified]`. `[unverified]` marks claims without any source. The remaining
sources ([G2], [G3], [G6], [G8]–[G13]) were not re-checked this pass and are still snippet-only.

## Summary

There are two different products:

1. **Gemini app (consumer, gemini.google.com):** Since a 2026-06-29 update, users can link **custom apps by MCP server URL** in Settings → Connected Apps → Custom apps, mainly for the **Gemini Spark** agent [G1], [G2], [G3]. Restrictions: 18+, US, **personal Google Account only** (no work/school accounts), Keep Activity on, English [G1]; added on web only, then usable on web and mobile [G1]. Auth is OAuth, officially DCR plus an "Advanced features" path where the user enters credentials by hand [G1]; write actions need the user's confirmation [G1]. A 2026-10-06 forum report describes a DCR + PKCE (S256) + RFC 9728 setup where Gemini never calls `/token` at all — the only recourse Google offers is "Report a problem" [G7] `[secondary]`.
2. **Gemini Enterprise (formerly Agentspace):** An **admin** creates a **custom MCP server data store** (preview, last updated 2026-10-09): Streamable HTTP only, TLS from a public CA, auth **No authentication** or **OAuth 2.0 with a manually registered confidential client** (client ID + secret, PKCE optional, scopes, an admin-run "Verify Auth" step); **no DCR, no OAuth discovery** [GE], [GB], [G8]. Users cannot add servers themselves; an admin enables up to 100 individual actions, all off by default until "Reload custom actions" [GE]. The Business edition's setup page has no PKCE field at all [GB].

For memory-manager: the consumer app is reachable only for US personal accounts, and a reported OAuth failure matches our own auth shape closely enough to be a real risk, not just noise. Gemini Enterprise works once the operator pre-registers a confidential client in memory-manager's AS (ADR-0015, `oauth-client create`); it was not possible before that command existed.

## MCP support

| Aspect | Gemini app (consumer) | Gemini Enterprise | Source |
|---|---|---|---|
| Who adds a server | The user, on the web app | Admin with `roles/discoveryengine.editor`; org policy override needed first | [G1], [GE], [GB] |
| Transport | "Must follow the standard MCP specifications" | Streamable HTTP only, TLS from a publicly trusted CA | [G1], [GE] |
| Read vs write | Write actions ask the user to confirm each call [G1] | Admin reloads and enables individual "actions" (tools), all off until "Reload custom actions"; max **100 enabled actions** per data store; `readOnlyHint: true` on a tool skips the per-call confirmation, otherwise the user confirms (`destructiveHint` keeps it explicit) | [GE] |
| Where tools run | Spark tasks and quick actions in chat; Spark reportedly needed for custom apps `[secondary]` | Agents in the Gemini Enterprise app; also importable from Agent Registry | [G1], [G9], [G10] |
| Server `instructions`, prompts, resources | `[unverified]` | `[unverified]` | — |
| Status | Rolling out since 2026-06-29 | Preview (page last updated 2026-10-09); VPC Service Controls needs explicit egress/data-source allowlisting, not supported at all for Business edition | [G2], [GE], [GB] |

## Auth

- **Consumer:** The user enters only the URL. Google documents OAuth via **dynamic client registration** as the default, plus an "Advanced features" path where the user enters a client's credentials by hand; work and school accounts are excluded entirely [G1]. Write actions need the user's confirmation per call [G1]. A forum report dated 2026-10-06 describes a setup — DCR, PKCE with `code_challenge_method=S256`, an RFC 9728 protected-resource response — where Gemini completes registration and the sign-in redirect, but never calls the authorization server's token endpoint at all ("Cannot Complete Request"); the only path Google offers from that screen is "Report a problem" [G7] `[secondary]`. That is the same shape memory-manager's own AS requires (DCR or a pre-registered client, PKCE S256 only, RFC 8707 `resource`), so the report is a real risk for the consumer app, not an unrelated edge case. Whether CIMD or static tokens are supported: `[unverified]`.
- **Enterprise:** No authentication, or OAuth 2.0 authorization code: admin enters Authorization URL (plus an optional, free-text "Authorization URL Parameters" field starting with `&`, documented for cases like `&access_type=offline&prompt=consent` or passing an `&audience=…`), Token URL, Client ID, Client Secret, an "Enable PKCE Support" checkbox (RFC 7636, optional, "recommended if your MCP server's OAuth provider supports PKCE"), and Scopes (space-separated; "a common scope is `offline_access`, which lets the app request a refresh token"); redirect URI is fixed to `https://vertexaisearch.cloud.google.com/oauth-redirect` (a Google product URL, not an operator value) [GE]. The admin's last setup step is **Verify Auth**. The token endpoint authentication method (Basic vs. POST) is not stated on the page [GE]. Each user may still need to authorize on first use [G11]. DCR and OAuth discovery not supported [G8] `[secondary]`. Cloud Run servers can instead receive a Google-signed ID token for IAM [GE]. The Business edition's equivalent page has no PKCE field at all [GB].

## Configuration

UI only on both. Enterprise: Google Cloud console → Gemini Enterprise → data stores → Custom MCP server → URL + auth (org policy constraint, `allowedEgressFqdns`/`allowedDataSources` for VPC-SC) → Actions → Reload custom actions → connect to an app → enable for users (disabled by default) [GE], [GB]. Business edition uses a "Connected apps" page under "Manage team", pre-GA [GB]; sources disagree whether Business supports custom MCP at all [G8] `[secondary]`.

## Built-in memory and extension points

Not an agent framework open to plugins. The consumer app has its own memory ("past chats", personal context) **[unverified for 2026 details]**. Integration is MCP only.

## Instructions / skills

No skill system found **[unverified]**. Enterprise offers no-code agents (Agent Designer) where an admin or user writes agent instructions that could carry the `memory_guide` workflow [G12] `[secondary]`.

## Org/admin controls

- Consumer: none; work and school accounts are excluded entirely [G1].
- Enterprise: admin-only setup; org policy constraint blocks custom MCP data stores by default; `roles/discoveryengine.editor` required; per-action enablement; organization-wide registration (not per user) [GE], [GB], [G13]. Agent Registry import makes the registry authoritative for tools [G10].

## Availability

- Consumer: US, 18+, personal Google accounts only, English, Keep Activity on, web for setup and web + mobile for use [G1]; Spark for Google AI Ultra and (US, English) AI Pro subscribers since 2026-07-16 [G3], [G9] `[secondary for Pro]`.
- Enterprise: Standard/Plus/Frontline editions; Business edition pre-GA with its own, simpler setup page [GB], [G8].

## Headless/CI usability

None for either product.

## Proposed support level

- **Gemini app (consumer): Not possible (reliably) today.** Restricted to US, 18+, personal, English-language accounts; a credible forum report describes exactly our auth shape failing at the token exchange; no work/school accounts, so it cannot be tested or rolled out here even where it works. Documented as not possible in [`../../clients/gemini-app.md`](../../clients/gemini-app.md); revisit triggers listed there.
- **Gemini Enterprise: Partial.** Works with Streamable HTTP and OAuth 2.0, but only with a **pre-registered confidential client** (client ID + secret) — built in `oauth-client create` (ADR-0015, #197). Admin-only, preview, ≤ 100 enabled actions, no profile registered yet (see [`../../clients/gemini-enterprise.md`](../../clients/gemini-enterprise.md)).

## Security notes (third-party message injection)

- Google states it does not control, monitor or secure third-party MCP servers, and data sent to them follows the server's privacy practice [G1].
- Spark runs autonomous, long-running tasks over e-mail, web and other connected apps; injected content in those sources could trigger `memory_write` without the user watching **[inference]**. A read-only token or tool subset is advisable for Spark.
- Enterprise: one admin-registered server serves the whole organisation; per-user OAuth and namespace permissions (ADR-0008) are needed so that agents of different users do not share one identity.

## Open questions

1. ~~Is a pre-registered confidential client (client ID + secret) something memory-manager's AS should support, given Gemini Enterprise needs it?~~ Resolved: yes, decided 2026-10-08 (ADR-0015), built in #197 (`oauth-client create`).
2. ~~Does the consumer custom-app OAuth flow use DCR, CIMD or both, and which redirect URI exactly?~~ Resolved as far as documented: DCR by default, or the user enters a client's credentials by hand under "Advanced features" [G1]; the exact redirect URI is still `[unverified]` — the only reported flow [G7] names a `googleusercontent.com` callback host but never reaches `/token`, so it cannot be confirmed end to end.
3. Do either product use MCP `instructions` or `prompts`? — still open, `[unverified]`.
4. ~~Edition matrix for Gemini Enterprise custom MCP (Business included or not).~~ Resolved: Business edition has its own setup page [GB] with no PKCE field and is marked pre-GA; Standard/Plus/Frontline use the page at [GE]. Whether Business's custom MCP support is otherwise equivalent is still `[unverified]`.
5. Does the consumer app ask for confirmation before write actions? — resolved: yes, per call [G1].

## Sources

- [G1] Connect & manage custom apps for Gemini Apps, https://support.google.com/gemini/answer/17209137, fully read, retrieved 2026-10-11 (search snippets only on 2026-10-07)
- [G2] What's new for Gemini Spark, https://support.google.com/gemini/answer/17171264 (search snippet only), retrieved 2026-10-07
- [G3] 9to5Google, "Gemini Spark now supports 3rd-party apps, including MCP", 2026-06-30, https://9to5google.com/2026/06/30/gemini-spark-apps-more/ (search snippet only), retrieved 2026-10-07
- [GE] Set up your custom MCP server data store, https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server, fully read, page says "Last updated 2026-10-09 UTC", retrieved 2026-10-11 (formerly [G4], search snippets only on 2026-10-07)
- [GB] Set up your custom MCP server connection – Gemini Enterprise (Business edition), https://support.google.com/g/answer/17106276, fully read, retrieved 2026-10-11 (formerly [G5], search snippets only on 2026-10-07)
- [G6] Pull request "connect Gemini custom apps over OAuth", https://github.com/erp-mafia/accounted/pull/3301 (search snippet only; page returned 404 on fetch), retrieved 2026-10-07
- [G7] Google AI Developers Forum, "Spark custom apps MCP OAuth never exchanges the code", https://discuss.ai.google.dev/t/gemini-app-spark-custom-apps-mcp-oauth-never-exchanges-the-code-cannot-complete-request-on-oauth-redirect-googleusercontent-com/186405, fully read `[secondary]` (forum thread, last reply dated 2026-10-06), retrieved 2026-10-11 (search snippet only on 2026-10-07)
- [G8] Preset MCP server authentication, https://docs.preset.io/docs/preset-mcp-server-authentication; SecureAuth "Connect Gemini Enterprise", https://docs.secureauth.com/ai/agents/gemini-enterprise/ (search snippets only), retrieved 2026-10-07
- [G9] 9to5Google, "Gemini Spark gets Workspace upgrades…", 2026-07-15, https://9to5google.com/2026/07/15/gemini-spark-workspace-upgrades/ (search snippet only), retrieved 2026-10-07
- [G10] Import MCP servers from Agent Registry, https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/import-govern-mcp-server-agent-registry (search snippet only), retrieved 2026-10-07
- [G11] Integrating Google SecOps into Gemini Enterprise using Custom MCP, https://security.googlecloudcommunity.com/community-blog-42/integrating-google-secops-into-gemini-enterprise-using-custom-mcp-7711 (search snippet only), retrieved 2026-10-07
- [G12] Getting started with BYO-MCP in Gemini Enterprise, https://medium.com/google-cloud/getting-started-with-byo-mcp-in-gemini-enterprise-building-a-no-code-gcp-q-a-agent-with-the-google-f61d67528d3c (title only), retrieved 2026-10-07
- [G13] Integrate Gemini Cloud Assist with third-party tools using MCP, https://docs.cloud.google.com/cloud-assist/configure-mcp (search snippet only), retrieved 2026-10-07
