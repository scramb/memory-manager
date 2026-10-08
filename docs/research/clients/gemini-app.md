# Client: Gemini app and Gemini Enterprise

Retrieved: 2026-10-07 · Tested version: none (desk research; latest: hosted services, no version)

**Source quality warning:** `support.google.com`, `docs.cloud.google.com` and `discuss.ai.google.dev` were blocked by the research environment's egress proxy. Statements below come from **search-engine snippets of the official pages** plus secondary sources, not from full reads. `[secondary]` marks claims backed only by third-party sources; `[unverified]` marks claims without a source. Re-check against the live pages before relying on them.

## Summary

There are two different products:

1. **Gemini app (consumer, gemini.google.com):** Since a 2026-06-29 update, users can link **custom apps by MCP server URL** in Settings → Connected Apps → Custom apps, mainly for the **Gemini Spark** agent [G1], [G2], [G3]. Restrictions: 18+, US, **personal Google Account only** (no work/school accounts), Keep Activity on; added on web only, then usable on web and mobile [G1]. Auth is OAuth with DCR per developer reports, and at least one report says the token exchange never completes [G6], [G7] `[secondary]`.
2. **Gemini Enterprise (formerly Agentspace):** An **admin** creates a **custom MCP server data store** (preview): Streamable HTTP only, HTTPS, auth **No authentication** or **OAuth 2.0 with a manually registered confidential client** (client ID + secret, PKCE optional); **no DCR, no OAuth discovery** [G4], [G5], [G8]. Users cannot add servers themselves.

For memory-manager: the consumer app is reachable only for US personal accounts, and its OAuth flow looks unreliable today. Gemini Enterprise works if the operator pre-registers a confidential client in memory-manager's AS — which the embedded AS does not offer today (it relies on DCR/CIMD) — or federates through Entra ID.

## MCP support

| Aspect | Gemini app (consumer) | Gemini Enterprise | Source |
|---|---|---|---|
| Who adds a server | The user, on the web app | Admin with Discovery Engine Editor; org policy override needed first | [G1], [G4], [G5] |
| Transport | "Must follow the standard MCP specifications" | Streamable HTTP only, HTTPS | [G1], [G4] |
| Read vs write | `[unverified]` | Admin reloads and enables individual "actions" (tools); max 100 enabled actions per data store | [G4] |
| Where tools run | Spark tasks and quick actions in chat; Spark reportedly needed for custom apps `[secondary]` | Agents in the Gemini Enterprise app; also importable from Agent Registry | [G1], [G9], [G10] |
| Server `instructions`, prompts, resources | `[unverified]` | `[unverified]` | — |
| Status | Rolling out since 2026-06-29 | Preview; VPC Service Controls not supported in preview | [G2], [G8] |

## Auth

- **Consumer:** The user enters only the URL; sign-in is OAuth with dynamic client registration and PKCE, with the callback relayed through a Google redirect host on `googleusercontent.com` [G6], [G7] `[secondary]`. A developer reports (forum, about 2026-10-02) that Gemini registers and the user signs in, but Gemini never calls the token endpoint ("Cannot Complete Request") [G7] `[secondary]`. Whether CIMD or static tokens are supported: `[unverified]`.
- **Enterprise:** No authentication, or OAuth 2.0 authorization code: admin enters authorization URL, token URL, client ID, client secret and scopes; `offline_access` suggested for refresh; PKCE optional; redirect URI `https://vertexaisearch.cloud.google.com/oauth-redirect` (a Google product URL, not an operator value) [G4], [G5]. Each user may still need to authorize on first use [G11]. DCR and OAuth discovery not supported [G8] `[secondary]`. Cloud Run servers can instead receive a Google-signed ID token for IAM [G4].

## Configuration

UI only on both. Enterprise: Google Cloud console → Gemini Enterprise → data stores → Custom MCP server → URL + auth → Actions → Reload custom actions → connect to an app → enable for users (disabled by default) [G4], [G5]. Business edition uses a "Connected apps" page under "Manage team" [G5]; sources disagree whether Business supports custom MCP at all [G8] `[secondary]`.

## Built-in memory and extension points

Not an agent framework open to plugins. The consumer app has its own memory ("past chats", personal context) **[unverified for 2026 details]**. Integration is MCP only.

## Instructions / skills

No skill system found **[unverified]**. Enterprise offers no-code agents (Agent Designer) where an admin or user writes agent instructions that could carry the `memory_guide` workflow [G12] `[secondary]`.

## Org/admin controls

- Consumer: none; work and school accounts are excluded entirely [G1].
- Enterprise: admin-only setup; org policy constraint blocks custom MCP data stores by default; IAM role required; per-action enablement; organization-wide registration (not per user) [G4], [G5], [G13]. Agent Registry import makes the registry authoritative for tools [G10].

## Availability

- Consumer: US, 18+, personal accounts, web for setup and web + mobile for use [G1]; Spark for Google AI Ultra and (US, English) AI Pro subscribers since 2026-07-16 [G3], [G9] `[secondary for Pro]`.
- Enterprise: Standard/Plus/Frontline editions (Business edition disputed) [G5], [G8].

## Headless/CI usability

None for either product.

## Proposed support level

- **Gemini app (consumer): Not possible (reliably) today.** Restricted to US personal accounts; OAuth token exchange reported broken; no work accounts. Revisit later.
- **Gemini Enterprise: Partial.** Works with Streamable HTTP and OAuth 2.0, but only with a **pre-registered confidential client** (client ID + secret). memory-manager's embedded AS would need static client registration, or the operator puts Entra ID in front (ADR-0006). Admin-only, preview.

## Security notes (third-party message injection)

- Google states it does not control, monitor or secure third-party MCP servers, and data sent to them follows the server's privacy practice [G1].
- Spark runs autonomous, long-running tasks over e-mail, web and other connected apps; injected content in those sources could trigger `memory_write` without the user watching **[inference]**. A read-only token or tool subset is advisable for Spark.
- Enterprise: one admin-registered server serves the whole organisation; per-user OAuth and namespace permissions (ADR-0008) are needed so that agents of different users do not share one identity.

## Open questions

1. Is a pre-registered confidential client (client ID + secret) something memory-manager's AS should support, given Gemini Enterprise needs it? (new auth-model surface → owner decision)
2. Does the consumer custom-app OAuth flow use DCR, CIMD or both, and which redirect URI exactly?
3. Do either product use MCP `instructions` or `prompts`?
4. Edition matrix for Gemini Enterprise custom MCP (Business included or not).
5. Does the consumer app ask for confirmation before write actions?

## Sources

- [G1] Connect & manage custom apps for Gemini Apps, https://support.google.com/gemini/answer/17209137 (search snippets only), retrieved 2026-10-07
- [G2] What's new for Gemini Spark, https://support.google.com/gemini/answer/17171264 (search snippet only), retrieved 2026-10-07
- [G3] 9to5Google, "Gemini Spark now supports 3rd-party apps, including MCP", 2026-06-30, https://9to5google.com/2026/06/30/gemini-spark-apps-more/ (search snippet only), retrieved 2026-10-07
- [G4] Set up your custom MCP server data store, https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server (search snippets only), retrieved 2026-10-07
- [G5] Set up your custom MCP server connection – Gemini Enterprise (Business edition), https://support.google.com/g/answer/17106276 (search snippets only), retrieved 2026-10-07
- [G6] Pull request "connect Gemini custom apps over OAuth", https://github.com/erp-mafia/accounted/pull/3301 (search snippet only; page returned 404 on fetch), retrieved 2026-10-07
- [G7] Google AI Developers Forum, "Spark custom apps MCP OAuth never exchanges the code", https://discuss.ai.google.dev/t/gemini-app-spark-custom-apps-mcp-oauth-never-exchanges-the-code-cannot-complete-request-on-oauth-redirect-googleusercontent-com/186405 (search snippet only), retrieved 2026-10-07
- [G8] Preset MCP server authentication, https://docs.preset.io/docs/preset-mcp-server-authentication; SecureAuth "Connect Gemini Enterprise", https://docs.secureauth.com/ai/agents/gemini-enterprise/ (search snippets only), retrieved 2026-10-07
- [G9] 9to5Google, "Gemini Spark gets Workspace upgrades…", 2026-07-15, https://9to5google.com/2026/07/15/gemini-spark-workspace-upgrades/ (search snippet only), retrieved 2026-10-07
- [G10] Import MCP servers from Agent Registry, https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/import-govern-mcp-server-agent-registry (search snippet only), retrieved 2026-10-07
- [G11] Integrating Google SecOps into Gemini Enterprise using Custom MCP, https://security.googlecloudcommunity.com/community-blog-42/integrating-google-secops-into-gemini-enterprise-using-custom-mcp-7711 (search snippet only), retrieved 2026-10-07
- [G12] Getting started with BYO-MCP in Gemini Enterprise, https://medium.com/google-cloud/getting-started-with-byo-mcp-in-gemini-enterprise-building-a-no-code-gcp-q-a-agent-with-the-google-f61d67528d3c (title only), retrieved 2026-10-07
- [G13] Integrate Gemini Cloud Assist with third-party tools using MCP, https://docs.cloud.google.com/cloud-assist/configure-mcp (search snippet only), retrieved 2026-10-07
