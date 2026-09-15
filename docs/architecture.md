# 模块架构

## module relationship

```text
Local CLI ───────────────┐
                         ├─ local Wiki compatibility boundary
Remote MCP ──────────────┤
                         ├─ reusable WikiCore (validate/organize)
Browser API ─────────────┘
                                  │
                          AuthService + Menti adapter
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
Menti identity → AuthService → MCP/browser request
                                  ↓
                    snapshot + model organization
                                  ↓
                 version/base check + one SQLite transaction
                                  ↓
              page + source + version + audit + idempotency record
```

Both MCP and browser reads query the same committed snapshot version. Clients
can submit content and source identifiers, but never a server filesystem path.

## status flow

```text
Menti member:     unknown → enabled → disabled
                                  ↑         │
                                  └─ newer authoritative reconciliation

MCP credential:   issued → valid → expired/revoked

Wiki submission:  received → organizing → validated → committed
                         └────── failed/invalid (no durable mutation)

Page version:     current → superseded
                         └─ restore creates a new current version
```

Member disablement is sticky against duplicate or older events. A newer
authoritative member reconciliation may change the state. Disablement never
deletes existing Wiki, source, version or audit records.
