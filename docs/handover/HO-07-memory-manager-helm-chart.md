> 🇩🇪 **Deutsch** (Leitfassung) · [🇬🇧 English](../readmes/en/handover/HO-07-memory-manager-helm-chart.md)

# HO-07 — memory-manager als generisches Helm-Chart, die Werte für tethys liegen in tethys-cluster

Status: übergeben · Version: 1 · Stand: 2026-10-18 · Termin: ohne Termin
Absender: `scramb/tethys-cluster` · ADR 0020 „Auslieferung als Helm-Chart“ · Empfänger: `scramb/memory-manager`
Offene Entscheidungen entscheidet: der Eigentümer beider Repos (scramb).

**Empfängerkopie, neutralisiert.** Die verbindliche Fassung liegt beim Absender in `scramb/tethys-cluster` unter `docs/handover/HO-07-memory-manager-helm-chart.md`. Weil dieses Repo öffentlich ist, sind hier alle Betreiberwerte durch Platzhalter ersetzt: Hostnamen, Identity-Provider, Vault-Remote und Gateway-Referenzen (`example.com`, `gateway-name`). Inhaltlich gilt dieselbe Vereinbarung.

## Anlass

tethys ist der Kubernetes-Cluster des Eigentümers. Flux wendet dort alles an, was in `scramb/tethys-cluster` steht. memory-manager läuft darauf öffentlich, unter einer Adresse des Betreibers.

Heute holt Flux die Kustomize-Basis `deploy/` aus `scramb/memory-manager`, festgelegt auf den Tag `v0.1.3`. tethys passt sie mit Patches an, die an Interna der Basis hängen: Containername, Namen der Umgebungsvariablen, JSON-Pfade. Ändert sich die Basis, bricht ein Patch, oder ein neuer Platzhalter läuft ungepatcht aus.

Der Eigentümer hat am 2026-10-18 festgelegt (ADR 0020 in tethys-cluster): Jede Anwendung liefert ein Helm-Chart aus ihrem eigenen Repo. Alles, was nur für tethys gilt, steht als Werte in `tethys-cluster`. Datenbanken und Secrets stellt tethys bereit.

Das Repo hat mit `charts/memory-manager` schon ein Chart, das den größten Teil davon kann. Dieser Katalog nennt die vier fehlenden Werte. Er nennt auch, was danach aus dem App-Repo verschwindet: die Kustomize-Basis und `deploy/flux/`, einen eigenen Flux-Aufbau, der nach dem Standard nicht ins App-Repo gehört.

Der Cluster wird gerade neu aufgebaut. Der Wechsel auf das Chart findet auf dem neuen Cluster statt. Bis zum Signal „HO-07: chart ready“ läuft memory-manager dort wie heute aus der Kustomize-Basis.

### Was tethys heute patcht und wo es künftig steht

| Heute per Patch gesetzt | Künftig | Im Chart `v0.1.3` |
|---|---|---|
| Pod-Label `istio.io/dataplane-mode: ambient`, mit dem der Pod dem Service-Mesh von tethys beitritt | zusätzliche Pod-Labels (AF-01) | fehlt |
| `OIDC_ALLOWED_EMAILS` aus einem Secret, denn die Allowlist ist persönlich und gehört nicht nach Git | Allowlist aus einem Secret-Schlüssel (AF-02) | fehlt, nur Klartext |
| `ALLOWED_ORIGINS` = `https://claude.ai,https://claude.com` | ein Wert (AF-03) | fehlt |
| `FORWARDED_ALLOW_IPS` = `127.0.0.1`, ausdrücklich gesetzt | ein Wert (AF-04) | fehlt |
| `PUBLIC_URL`, `VAULT_REMOTE`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `LOGIN_NAMESPACES` | `publicUrl`, `vault.remote`, `login.oidc.*`, `login.namespaces` | vorhanden |
| `EMBEDDING_PROVIDER` = `none` | `embedding.provider: none` | vorhanden |
| HTTPRoute `parentRefs` und `hostnames` | `httpRoute.*` | vorhanden |
| Datenbank: CNPG-Cluster `memory-manager-db` aus der Basis, StorageClass per Patch | ganz bei tethys, das Chart liest nur die Verbindung (`database.cnpg.enabled: false`, `database.existingSecret`) | vorhanden |
| `ExternalSecret` aus der Basis, Store und Pfade per Patch | ganz bei tethys, das Chart verweist auf das Secret (`secrets.existingSecret`) | vorhanden |
| Image-Tag `0.1.3` | kein Wert, es gilt `appVersion` am gepinnten Tag | vorhanden |

## Nutzen

tethys betreibt memory-manager aus dem generischen Chart des App-Repos, mit allen tethys-Werten in `tethys-cluster`: Das App-Repo enthält nichts mehr, was nur für tethys gilt, und tethys patcht keine Interna mehr.

**Fertig, wenn:** tethys das Issue „HO-07: switched“ in `scramb/memory-manager` geschlossen hat (siehe Integration) und am aktuellen Tag des App-Repos weder `deploy/flux/` noch die Kustomize-Basis existiert.

## Anforderungen

Reihenfolge = empfohlene Abarbeitungsreihenfolge. Muss: ohne das keine Integration · Soll: vor „Fertig, wenn“ gebraucht · Kann: blockiert nichts, darf später kommen.

Jede Anforderung zu einem neuen Wert (AF-01 bis AF-04) liefert vier Dinge:
- den Wert selbst,
- einen Eintrag in `values.schema.json`,
- eine Zeile in der README des Charts,
- einen Render-Nachweis mit `helm template memory-manager <chart-pfad> -n memory-manager -f <datei>`.

Die Namen der Werte sind Vorschläge. Die verbindlichen legt der Empfänger fest.

| ID | Anforderung | Abnahmekriterium (von außen prüfbar) | Priorität | Hängt ab von |
|---|---|---|---|---|
| AF-01 | Das Chart setzt zusätzliche Pod-Labels aus einem Wert (`podLabels`). | Mit `podLabels: {istio.io/dataplane-mode: ambient}` trägt das Pod-Template dieses Label neben `app.kubernetes.io/name: memory-manager`. Mit `podLabels: "x"` scheitert das Rendern am Schema. | Muss | — |
| AF-02 | Die Allowlist lässt sich aus einem Schlüssel des Secrets `secrets.existingSecret` lesen (z. B. `login.oidc.allowedEmailsSecretKey`). | Mit dem Schlüssel gesetzt und `allowedEmails: ""` rendert `OIDC_ALLOWED_EMAILS` als `secretKeyRef` auf `memory-manager-secrets`/`OIDC_ALLOWED_EMAILS`. Sind beide nicht leer, scheitert das Rendern mit einer Fehlermeldung. Der Klartextwert `allowedEmails` darf bleiben. Fehlt der Schlüssel im Secret, endet der Pod in `CreateContainerConfigError`. Das ist akzeptiert. | Muss | — |
| AF-03 | Das Chart setzt `ALLOWED_ORIGINS` aus einem Wert (z. B. `allowedOrigins`). | Mit `allowedOrigins: https://claude.ai,https://claude.com` steht genau dieser Wert in der Umgebung. Ist der Wert leer, fehlt die Variable. | Muss | — |
| AF-04 | Das Chart setzt `FORWARDED_ALLOW_IPS` aus einem Wert (z. B. `forwardedAllowIps`). | Mit `forwardedAllowIps: 127.0.0.1` steht genau dieser Wert in der Umgebung. Ist der Wert leer, fehlt die Variable. | Muss | — |
| AF-05 | Ein veröffentlichter Tag rendert das vollständige Beispiel aus dem Kontrakt. | Das Signal „HO-07: chart ready“ nennt Tag und Chart-Pfad. Das Image `ghcr.io/scramb/memory-manager:<appVersion>` ist zu diesem Zeitpunkt abrufbar. `helm template` mit dem Beispiel liefert genau, was unter „Erwartete Ausgabe“ steht. | Muss | AF-01 bis AF-04 |
| AF-06 | Das App-Repo enthält keine Flux-Objekte mehr. | Am nächsten Tag gibt es kein `deploy/flux/`, und das Kommando unter der Tabelle liefert nichts. | Soll | — |
| AF-07 | Die Kustomize-Basis `deploy/` ist entfernt, nachdem tethys umgestellt hat. | Am ersten Tag nach „HO-07: switched“ gibt es kein `deploy/kustomization.yaml`. | Soll | AF-05, Signal „HO-07: switched“ |
| AF-08 | Die CI rendert das Chart mit einer Wertedatei, die dieselben Schlüssel wie das Beispiel nutzt, mit neutralen Platzhaltern. | Die Datei liegt unter `charts/memory-manager/ci/`. Ein öffentlicher CI-Lauf rendert sie grün. | Kann | AF-05 |

Nachweis für AF-06, aus der Wurzel des App-Repos:

```bash
git grep -nE 'kind: (HelmRelease|HelmRepository|GitRepository|OCIRepository)|kustomize\.toolkit\.fluxcd\.io' -- '*.yaml' '*.yml' ':!*.md'
```

## Schnittstellen-Kontrakt

- **Richtung und Auslöser:**
  - Flux in tethys holt das Chart aus `https://github.com/scramb/memory-manager`, festgelegt auf einen Tag, und installiert es mit den Werten aus tethys-cluster.
  - Release-Name und Ziel-Namespace sind beide `memory-manager`.
  - Ein Rollout ist ein Commit in tethys, der den Tag hebt. Das App-Repo pusht nichts in den Cluster.
- **Datenform:** Die Schnittstelle sind die Werte des Charts. Hier das Beispiel der Werte von tethys:

```yaml
fullnameOverride: memory-manager
serviceAccount:
  create: false
podLabels:                                  # new (AF-01)
  istio.io/dataplane-mode: ambient
publicUrl: https://memory.example.com
vault:
  remote: git@git.example.com:owner/vault.git
  branch: main
login:
  mode: oidc
  oidc:
    issuer: https://idp.example.com/
    clientId: memory-manager
    allowedEmails: ""
    allowedEmailsSecretKey: OIDC_ALLOWED_EMAILS   # new (AF-02)
  namespaces: "*"
allowedOrigins: https://claude.ai,https://claude.com   # new (AF-03)
forwardedAllowIps: 127.0.0.1                # new (AF-04)
embedding:
  provider: none
secrets:
  existingSecret: memory-manager-secrets
database:
  cnpg:
    enabled: false
  existingSecret:
    name: memory-manager-db-app
    key: uri
httpRoute:
  enabled: true
  parentRefs:
    - name: gateway-name
      namespace: gateway-namespace
      sectionName: https
  hostnames:
    - memory.example.com
```

- **Erwartete Ausgabe** von `helm template` mit diesem Beispiel (Prüfliste für AF-05):
  - **Objekte:** genau ein `Deployment`, ein `Service` und eine `HTTPRoute`, alle mit Namen `memory-manager`. Es gibt kein `Secret`, keinen `ServiceAccount`, keinen CNPG-`Cluster`, kein `Namespace`, kein `Ingress`, keine `NetworkPolicy`, keinen `ServiceMonitor` und kein Flux-Objekt.
  - **Pod-Labels:** `app.kubernetes.io/name: memory-manager` und `istio.io/dataplane-mode: ambient`.
  - **Image:** `ghcr.io/scramb/memory-manager:<appVersion des Tags>`.
  - **Umgebung mit festen Werten:**
    - `PUBLIC_URL=https://memory.example.com`
    - `VAULT_REMOTE=git@git.example.com:owner/vault.git`
    - `VAULT_BRANCH=main`
    - `LOGIN_MODE=oidc`
    - `OIDC_ISSUER=https://idp.example.com/`
    - `OIDC_CLIENT_ID=memory-manager`
    - `LOGIN_NAMESPACES=*`
    - `ALLOWED_ORIGINS=https://claude.ai,https://claude.com`
    - `FORWARDED_ALLOW_IPS=127.0.0.1`
  - **Umgebung aus Secrets:** `DATABASE_URL` aus `memory-manager-db-app`/`uri`. `OIDC_ALLOWED_EMAILS`, `OIDC_CLIENT_SECRET`, `VAULT_WEBHOOK_SECRET` und `OAUTH_CLIENT_SECRET_KEY` aus `memory-manager-secrets`.
  - **Volume** `ssh` aus `memory-manager-secrets`, mit `SSH_PRIVATE_KEY` und `SSH_KNOWN_HOSTS`.
  - **Kein** `EMBEDDING_PROVIDER`.
  - **HTTPRoute:** `parentRefs` und `hostnames` wie im Beispiel, Backend `memory-manager:8080`.
- **Secrets, die tethys bereitstellt:** Das Chart verweist nur auf sie.
  - `memory-manager-secrets`: Die Schlüssel `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`, `VAULT_WEBHOOK_SECRET`, `OIDC_CLIENT_SECRET` und `OAUTH_CLIENT_SECRET_KEY` erwartet das Chart `v0.1.3` schon (`templates/deployment.yaml`). `OIDC_ALLOWED_EMAILS` kommt mit AF-02 dazu.
  - `memory-manager-db-app` mit dem Schlüssel `uri`, die Verbindung zum CNPG-Cluster von tethys.
- **Namen, auf die tethys sich verlässt:** das Pod-Label `app.kubernetes.io/name: memory-manager`, auf das die Netzwerkfreigabe von tethys zielt (nur das öffentliche Gateway darf den Pod erreichen), und die Objektnamen `memory-manager`. Der Selector des Charts (`name` und `instance`) bleibt, wie er ist.
- **Fehlerverhalten:**
  - Scheitert Rendern oder Installation, zeigt das `HelmRelease` `Ready False` mit der Ursache, und der zuletzt installierte Stand läuft weiter.
  - Fehlgeschlagene Upgrades wiederholt tethys nicht.
  - Ist das App-Repo nicht erreichbar, läuft die App mit dem letzten Stand weiter.
- **Kompatibilität:**
  - Ein Bruch ist ein umbenannter oder entfernter Schlüssel, ein neuer Pflicht-Schlüssel im Secret, ein neuer Port oder eine neue Verbindung nach außen. So ein Bruch geht erst auf den ausgelieferten Branch, wenn tethys in einem Issue im App-Repo bestätigt hat, dass die neuen Werte bereitliegen.
  - Ein Bruch hebt solange `0.x` gilt mindestens die Minor-Version, danach die Major-Version, und er steht im `CHANGELOG.md`.
  - Ein bestehender Tag ändert sich nie.
- **Verbindliche Fassung:** `values.yaml` und `values.schema.json` des Charts am jeweiligen Tag. tethys hält nur seine eigenen Werte. Bis AF-05 geliefert ist, gilt dieser Abschnitt.

## Nicht-Scope

- Die Veröffentlichung des Charts als OCI-Artefakt. Sie darf bleiben, tethys nutzt sie nicht.
- Die Optionen `database.cnpg`, `secrets.create`, `ingress`, `networkPolicy` und `serviceMonitor`. Sie bleiben für andere Betreiber, wie sie sind. tethys schaltet sie nicht ein.
- Namespace, Secrets, Datenbank, Netzwerkfreigabe und die Übernahme bestehender Objekte. Das bleibt alles bei tethys.
- Image-Automation und Änderungen am Verhalten der App.

## Randbedingungen

Entschieden vom Eigentümer am 2026-10-18 (ADR 0020). Nicht verhandelbar.

- **Ablage:** Das Chart liegt im App-Repo. Flux verbraucht es über eine Git-Quelle auf dieses Repo mit einem `HelmRelease`. Den Pfad wählt der Empfänger.
- **Keine umgebungsgebundenen Werte:** Das Chart enthält keine Werte, die nur in einer Umgebung gelten: keinen Hostnamen, keine Gateway-Referenz, keine StorageClass und keinen Namen einer Betreiberumgebung. Prüfbar daran, dass kein Name einer Umgebung des Betreibers vorkommt. Es enthält kein Namespace-Objekt und keine Flux-Objekte.
- **Image-Tag:** Der Default gehört dem Chart (`appVersion`). tethys pinnt über den Git-Tag. Ein Tag erscheint erst, wenn sein Image in der Registry liegt.
- **Secrets:** Mit den Werten von tethys erzeugt das Chart kein Secret. Die Erzeugung aus Werten bleibt wie heute ein ausdrückliches Opt-in mit Standard aus (`secrets.create: false`).
- **Datenbank:** Sie gehört tethys. Das Chart liest die Verbindung aus einem Secret, dessen Name ein Wert ist.
- **Einmaliger Neuaufbau beim Wechsel:** Deployment, Service und HTTPRoute dürfen beim Wechsel einmal neu angelegt werden. Das bedeutet rund eine Minute Ausfall.
- **Zielplattform, Stand 2026-10-18:**
  - Kubernetes 1.36.2,
  - Gateway API v1.5.1 (`HTTPRoute` `gateway.networking.k8s.io/v1`),
  - Flux v2.9.4 mit `HelmRelease` `helm.toolkit.fluxcd.io/v2`, gerendert vom Helm des helm-controllers,
  - CloudNativePG für die Datenbank, betrieben von tethys.

## Annahmen

| ID | Annahme | Grundlage | Wenn falsch |
|---|---|---|---|
| A-01 | Fehlt `EMBEDDING_PROVIDER`, verhält sich die App wie bei `none`. Das Chart lässt die Variable bei `none` weg | Template `deployment.yaml` in `v0.1.3` | AF-05 braucht die Variable ausdrücklich |

## Offene Entscheidungen

| Frage | Optionen | Blockiert | Entscheidet |
|---|---|---|---|
| F-01: Welche Daten in der Datenbank lassen sich **nicht** aus dem Vault neu aufbauen? Das ist wichtig für Dump und Restore beim Neuaufbau des Clusters. Laut den Kommentaren der Migrationen `0002_static_tokens.sql` und `0003_oauth.sql` in `v0.1.3` sind `static_tokens`, `oauth_clients`, `oauth_pending`, `oauth_auth_codes` und `oauth_tokens` Betriebsdaten, „never derivable from the vault“ | A: nur der Suchindex ist neu aufbaubar, alles andere muss mit dem Dump umziehen · B: eine andere Liste, die der Empfänger nennt. Bis zur Antwort behandelt tethys die ganze Datenbank als nicht neu aufbaubar und stellt sie vollständig wieder her. Gingen diese Tabellen beim Restore verloren, müssten sich alle OAuth-Clients neu registrieren, und alle statischen Tokens müssten neu ausgegeben werden | keine AF, nur den Restore bei tethys | Empfänger antwortet |

Die Fragen zu Datenbank, Selector und Secret-Erzeugung hat der Eigentümer am 2026-10-18 entschieden. Sie stehen unter Randbedingungen.

## Integration

Umstellungsschritt in tethys-cluster: [`scramb/tethys-cluster#3`](https://github.com/scramb/tethys-cluster/issues/3).

Wenn AF-05 geliefert ist, eröffnet der Empfänger in `scramb/tethys-cluster` ein Issue „HO-07: chart ready“. Es nennt Tag, Chart-Pfad und die erfüllten AFs.

tethys stellt dann auf dem neu aufgebauten Cluster um:
- Es legt Datenbank und Secrets bereit.
- Es löscht die alten Objekte `Deployment`, `Service` und `HTTPRoute` aus der Kustomize-Auslieferung, bevor das `HelmRelease` installiert. Das ist wegen des anderen Selectors nötig, rund eine Minute Ausfall.
- Es installiert das Chart am genannten Tag.

Danach stellt tethys von außen nach:
- Das `HelmRelease` zeigt `Ready True`.
- `https://memory.example.com/healthz` liefert `200`.
- `/mcp` ohne Token liefert `401`.
- `/.well-known/oauth-protected-resource/mcp` nennt `resource` `https://memory.example.com/mcp`.
- Der Login gelingt im Browser.
- Der Connector in claude.ai arbeitet weiter, ohne sich neu zu registrieren.

Ist das erfüllt, meldet tethys das mit einem Issue „HO-07: switched“ in `scramb/memory-manager`. Damit sind AF-06 und AF-07 frei.

## Änderungen

| Version | Datum | Was |
|---|---|---|
| 1 | 2026-10-18 | Entwurf. Entscheidungen des Eigentümers aus ADR 0020 und Befunde des Kreuzverhörs eingearbeitet; übergeben |
