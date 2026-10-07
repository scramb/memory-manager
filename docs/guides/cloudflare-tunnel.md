# Exposing memory-manager via Cloudflare Tunnel

An alternative to the public `HTTPRoute`/`Ingress` `deploy/README.md` and
`charts/memory-manager/README.md` ship: `cloudflared` opens an outbound-only connection from
wherever the server runs to Cloudflare's edge, so nothing needs to open an inbound port - no
Gateway API controller, no `LoadBalancer` Service, no port-forward on a router. Useful behind a
NAT, on a cluster with no ingress controller installed yet, or for a quick trial without touching
firewall rules.

Checked against `cloudflared --version` **2026.10.0** on **2026-10-07**, every command below run
against that binary's own `--help` output; the ingress-rule configuration-file shape against
<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/>, retrieved the
same day.

## When to use a tunnel instead of the public ingress

- No Gateway API controller or Ingress controller is installed, and adding one is more than this
  deployment needs.
- The server runs behind a NAT or on a network with no public IP at all (a home lab, a VPS without
  a floating IP).
- An existing Cloudflare zone and account are already in place for other purposes - one more
  tunnel is cheaper than a new ingress path.

It does **not** replace anything about memory-manager's own auth: the embedded OAuth
authorization server (ADR-0004) still handles every `/authorize`/`/token` exchange, a tunnel only
changes how traffic reaches the server's own port. `PUBLIC_URL` still has to equal exactly the
hostname a client is told, canonical form - same rule as behind a real ingress.

## Prerequisites

- A Cloudflare account with a zone (a domain whose nameservers point at Cloudflare) - the tunnel's
  public hostname becomes a subdomain of it, `memory.example.com` throughout this guide is a
  placeholder for that.
- `cloudflared` installed once, wherever the tunnel is created and managed from (an operator's own
  workstation, not necessarily where memory-manager runs):
  <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>.
- memory-manager already reachable on a private network: the compose quickstart (`compose.yaml`)
  or a cluster Service (`deploy/service.yaml`, or the Helm chart's own Service) - a tunnel proxies
  to that existing address, it is not a replacement for running the server itself.

## Create the tunnel (one-time)

```sh
cloudflared tunnel login
cloudflared tunnel create memory-manager
cloudflared tunnel route dns memory-manager memory.example.com
```

`login` opens a browser to authorize `cloudflared` against the account and writes a certificate
(`cert.pem`). `create` registers the tunnel with Cloudflare, prints its UUID, and writes a
credentials JSON file (default `~/.cloudflared/<UUID>.json`) - keep that file private, same
category as the vault deploy key in `deploy/README.md`'s own "Required secrets" table: whoever
holds it can run traffic through this tunnel. `route dns` adds the CNAME record that points
`memory.example.com` at the tunnel.

## Running it

### Alongside compose

Add a `cloudflared` service to an operator's own copy of `compose.yaml` (not this repository's -
CLAUDE.md: deployment artefacts stay generic, no operator-specific values committed here) pointing
at the `memory-manager` service already on the compose network:

```yaml
services:
  cloudflared:
    image: docker.io/cloudflare/cloudflared:latest
    command: tunnel --config /etc/cloudflared/config.yml run
    volumes:
      - ./cloudflared/config.yml:/etc/cloudflared/config.yml:ro
      - ./cloudflared/<TUNNEL-UUID>.json:/etc/cloudflared/<TUNNEL-UUID>.json:ro
    depends_on:
      - memory-manager
```

```yaml
# ./cloudflared/config.yml
tunnel: <TUNNEL-UUID>
credentials-file: /etc/cloudflared/<TUNNEL-UUID>.json
ingress:
  - hostname: memory.example.com
    service: http://memory-manager:8080
  - service: http_status:404
```

The catch-all `http_status:404` rule at the end is required (`cloudflared`'s own ingress-rule
validation rejects a config file without one) - it is what every request to a hostname this tunnel
does not route gets instead of falling through. Validate the file before running it:

```sh
cloudflared tunnel ingress validate --config ./cloudflared/config.yml
cloudflared tunnel ingress rule --config ./cloudflared/config.yml https://memory.example.com/mcp
```

Set `PUBLIC_URL` on the `memory-manager` service itself to the tunnel's own public hostname
(`compose.yaml`'s own `PUBLIC_URL: http://localhost:8080` is the quickstart default for *no*
tunnel in front - override it in `.env` or the service's own `environment:` once one is):
`PUBLIC_URL=https://memory.example.com`, never the compose-internal
`http://memory-manager:8080` the `ingress:` rule above proxies to.

### As a Deployment in Kubernetes

Rather than mount the credentials JSON file as a Secret volume, fetch a tunnel *token* once and
run with `--token` instead - a single string, simpler to carry as one `Secret` key than a JSON
file:

```sh
cloudflared tunnel token memory-manager
kubectl -n memory-manager create secret generic cloudflared-token --from-literal=TUNNEL_TOKEN=<token>
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflared
  namespace: memory-manager
spec:
  replicas: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: cloudflared
  template:
    metadata:
      labels:
        app.kubernetes.io/name: cloudflared
    spec:
      containers:
        - name: cloudflared
          image: docker.io/cloudflare/cloudflared:latest
          args: ["tunnel", "--no-autoupdate", "run"]
          env:
            - name: TUNNEL_TOKEN
              valueFrom:
                secretKeyRef:
                  name: cloudflared-token
                  key: TUNNEL_TOKEN
```

Two replicas is fine here: `cloudflared` itself is stateless, each replica is just another
connector for the same tunnel (`cloudflared tunnel info memory-manager` lists every active one).
This is unrelated to memory-manager's own single-writer constraint
(`charts/memory-manager/values.yaml`'s own comment on `replicaCount`) - that Deployment stays at
0 or 1 regardless of how many `cloudflared` connectors sit in front of it.

A `--token`-run tunnel reads its ingress rules from the dashboard instead of a mounted
`config.yml`: one.dash.cloudflare.com → the tunnel → Public Hostname, hostname
`memory.example.com` → service `http://memory-manager.memory-manager.svc.cluster.local:8080` (the
Service `deploy/service.yaml`/the Helm chart's own `templates/service.yaml` already provide, same
cluster-internal DNS name an `HTTPRoute` would otherwise point a Gateway at) - deliberately not
this Deployment's own `kustomization.yaml`/chart `values.yaml`, since choosing a tunnel at all is
an alternative to those resources' `httpRoute`/`ingress` blocks, not a value of them.

## MCP-specific pitfalls

From `docs/research/mcp-auth-and-connectors.md` §4 - these apply to a tunnel exactly as they would
to a direct ingress, but a tunnel usually sits behind Cloudflare's own WAF/Access/Bot products,
which is where they tend to surface as a silent connection failure instead of an obvious error:

- claude.ai's requests come from Anthropic's cloud, not the user's device, from the fixed egress
  range `160.79.104.0/21`. A Cloudflare Access policy or WAF custom rule in front of this hostname
  must let that range through for `/mcp`, every OAuth endpoint (`/authorize`, `/token`,
  `/register`, `/revoke`) and `/.well-known/*` - otherwise claude.ai's connection fails before it
  ever reaches memory-manager, with nothing in this server's own logs to show why.
- **Never** put Cloudflare Access's own login page in front of `/mcp`. claude.ai is a machine
  client; it cannot click through an Access login form. If Access protects anything on this
  hostname, add a bypass policy for `/mcp`, the OAuth endpoints and `/.well-known/*` - the embedded
  authorization server (ADR-0004) is this server's own login, Access's is a second, incompatible
  one in front of it.
- Bot Fight Mode (and Super Bot Fight Mode) is tuned to catch exactly what claude.ai's
  server-to-server calls look like: no browser, no cookies, a fixed IP range making many requests.
  Add a WAF skip rule for the same set of paths above, or it returns a challenge page where a
  401/200 JSON response belongs, which looks like a server bug from memory-manager's side.
- The embedded authorization server's `/authorize`/`/token`/`/register` must answer well under
  claude.ai's 10 s discovery/registration/token timeout (ADR-0004's own consequence). A tunnel adds
  one more hop - normally low milliseconds, but worth checking once under the "Monitor tunnels" →
  "Log streams"/"Metrics" dashboard view if sign-in ever feels slow.
- `PUBLIC_URL` must equal exactly the hostname a user enters into claude.ai's connector dialog,
  canonical form (scheme + host, no trailing slash, no default port) - the tunnel's own public
  hostname (`memory.example.com` above), never the compose-internal or cluster-internal service
  address the `ingress:` rule/dashboard Public Hostname proxies to (ADR-0004: "claude.ai sends the
  canonical server URL as `resource`").
- The vault webhook (`/hooks/vault`, `deploy/README.md`'s own "Webhook setup") sits behind this
  same hostname once a tunnel replaces direct ingress. Whatever bypass rule the bullets above add
  for `/mcp`/OAuth/`.well-known` needs its own entry for `/hooks/vault` too, or GitHub/Gitea's
  webhook requests hit the same Bot Fight Mode/Access checks and never trigger a sync.
- Streamable HTTP over a tunnel: `cloudflared` proxies both a plain HTTP response and a long-lived
  SSE stream, but the latter depends on the tunnel surviving an edge reconnect mid-stream, which a
  one-shot JSON response does not need to. `MCP_JSON_RESPONSE=true` is already this server's own
  default (`src/memory_manager/config.py`, see `docs/research/bring-mcp-reference.md`) for exactly
  this reason - nothing to set here, just worth knowing why a long-lived stream is not the default
  shape a request behind this tunnel takes.

## Verify

Same shape as `deploy/README.md`'s own "Checks":

```sh
cloudflared tunnel info memory-manager
curl -s https://memory.example.com/healthz
curl -s https://memory.example.com/.well-known/oauth-protected-resource
curl -s -o /dev/null -w '%{http_code}\n' https://memory.example.com/mcp   # 401 without a token
```

## Troubleshooting

- **Tunnel shows no active connector** (`cloudflared tunnel info memory-manager` lists none): the
  `cloudflared` process never started successfully - check its own logs (`--loglevel debug` is
  verbose but shows every connection attempt) before assuming the problem is in memory-manager.
- **`404` from Cloudflare, not from memory-manager**: the request hit the catch-all ingress rule,
  meaning no `hostname:` rule above it matched - check the exact hostname in `config.yml`/the
  dashboard's Public Hostname entry against what was requested
  (`cloudflared tunnel ingress rule <url>` shows which rule a given URL matches, without needing a
  live request).
- **claude.ai's connector dialog fails at the "Discovering" step**: almost always the egress-range
  or Bot Fight Mode bullets above - a WAF/Access event log entry for a request from
  `160.79.104.0/21` to `/.well-known/oauth-authorization-server` or `/.well-known/mcp-resource`
  returning something other than 200 is the place to look first.
- **Sign-in works in a browser but claude.ai still reports "unauthorized"**: Access sat in front of
  `/mcp` or an OAuth endpoint after all - re-check the bypass policy covers every path the bullets
  above list, not just `/mcp` itself.

## Sources

- `cloudflared tunnel --help`, `cloudflared tunnel run --help`, `cloudflared tunnel create --help`,
  `cloudflared tunnel token --help`, `cloudflared tunnel route dns --help`,
  `cloudflared tunnel ingress --help` - cloudflared version 2026.10.0, retrieved 2026-10-07.
- <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/> - retrieved
  2026-10-07 (the `tunnel`/`credentials-file`/`ingress` configuration-file shape).
- [`docs/research/mcp-auth-and-connectors.md`](../research/mcp-auth-and-connectors.md) §4 -
  claude.ai's egress range and OAuth timing/canonical-URL rules.
- [`deploy/README.md`](../../deploy/README.md) - "Checks" and "Webhook setup" sections this guide's
  own verification and webhook notes mirror.

**Verified: not yet - tracked in #47**
