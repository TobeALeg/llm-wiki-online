# 模块架构

## module relationship

```text
Local CLI ───────────────┐
                         ├─ local Wiki compatibility boundary
Remote MCP ──────────────┤
                         ├─ reusable WikiCore (validate/organize)
Browser API ─────────────┘
                                  │
                  OAuthService → AuthService + mentti adapter
                                  │
                          SharedWikiStore (SQLite)
                                  │
                    pages / sources / versions / audit / idempotency
```

`WikiCore` is side-effect free. It accepts selected material, existing Wiki
context and a purpose, then returns a validated update package. The local CLI
keeps its existing file-based behavior; remote adapters do not receive a
server path or a command.

`AuthService` owns authorization-code redemption, sessions, MCP credentials,
member status and ordered webhook/reconciliation updates. `SharedWikiStore`
owns the one company Wiki and commits its durable records atomically.

`OAuthService` is the MCP authorization-server seam. It owns protected-resource
and authorization-server metadata, dynamic client registration, authorization
consent, PKCE code exchange, refresh-token rotation and revocation. It delegates
member identity to `AuthService`; mentti does not need to become a general OAuth
provider. OAuth access tokens and browser-generated personal MCP Keys both enter
the same subject-bound MCP token verifier.

## data flow

### local mode

```text
Agent-selected data → authenticated request → WikiCore → update package → Agent local write-back
                                           └──────── no durable store, log, queue, or backup ────────┘
```

The request may be sent to the configured model provider. Provider retention
rules are not inferred by lw and remain an explicit operational concern.

### company mode

```text
mentti identity → OAuthService/AuthService → MCP/browser request
                                  ↓
                    snapshot + model organization
                                  ↓
                 version/base check + one SQLite transaction
                                  ↓
              page + source + version + audit + idempotency record
```

Both MCP and browser reads query the same committed snapshot version. Only the
authenticated MCP write tools commit company content; the browser API is
read-only. Clients can submit content and source identifiers, but never a
server filesystem path.

## status flow

```text
mentti member:     unknown → enabled → disabled
                                  ↑         │
                                  └─ newer authoritative reconciliation

OAuth grant:       requested → mentti login → consented → code → access + refresh
refresh token:     valid → rotated/revoked
personal MCP Key:  issued once → valid → expired/revoked

Wiki submission:  received → organizing → validated → committed
                         └────── failed/invalid (no durable mutation)

Page version:     current → superseded
                         └─ restore creates a new current version
```

Member disablement is sticky against duplicate or older events. A newer
authoritative member reconciliation may change the state. Disablement never
deletes existing Wiki, source, version or audit records.
