# Native metadata ingress (Fork candidate)

Pinned base: LangBot 4.11.0, plugin SDK 0.7.11. This code is a deployment
candidate, not evidence that either source is enabled or that a business task
completed.

## One consumer and three permission boundaries

The two native adapters durably accept source metadata and feed the existing
Router Host. They do not emit an ordinary LangBot pipeline event or run a second
Router. Source identity, record, version and original reference remain owned by
the existing source. Personal WeChat owner frame sequence, raw wx cursor and Core
delivery position are separate domains; Finance sequence is per record.

The service source profile grants metadata ingestion only. OpenAgents original
readers retain their separate purpose, agent, audience, content-type and sharing
checks. Neither a Bot configuration nor a credential reference grants access.
Account-wide wx read/media credentials must stay with the original source owner;
they cannot be relabelled as a thread-scoped credential.

A dedicated persisted API key with **explicit empty scopes** and an exact enabled
receiver registration consumes the three internal HTTP routes. The default policy
rejects global/legacy, high-scope, expired, revoked and unregistered keys. The
single-consumer page/ACK/receipt protocol is intentionally absent from user MCP
operations: it is not an agent-accessible HTTP capability. Existing user tools
retain their ordinary HTTP/MCP parity.

Human metadata visibility is separate again: current Workspace membership and
`resource.view` are necessary, but an explicitly approved observer registration
and current source audience proof are also required. Service reader access does
not implicitly authorize every Workspace member. Counts and original identifiers
are withheld on missing or revoked audience proof. No original body is stored in
native metadata or exposed by this UI.

## Existing Bot workflow

Use the existing adapter gallery to create a disabled, unconfigured draft. Save a
complete opaque binding and credential reference while still disabled. A normal
account with the existing resource/API-key management permissions registers the
exact source approval, receiver identity, observer list and expected revision;
registration never accepts a raw credential. Only then enable the original Bot
switch. Source credentials and endpoints live in the separately controlled
service registry, not Bot JSON. Source and receiver defaults remain disabled.

The existing detail/status and logs area shows durable source acceptance and
Router producer acceptance. `router_accepted` does **not** mean a business action
executed or a reply was delivered. Ordinary event routing, plugin processing and
simulated event debug are unavailable for these ingress-only adapters. No new
business dashboard is required.

Successful metadata polling can show `source_available`; it cannot prove the
WeChat account is logged in. `connected` requires fresh native collector proof.
Missing/stale collector proof remains unknown or awaiting upstream. No records
on an unauthorized page must not be presented as a successful empty result.

## Recovery and rollout gates

Source page acceptance and upstream checkpoint commit atomically. The Host commits
its workflow candidate, coverage and ACK-pending receipt before acknowledging the
frozen Core page. A lost ACK response is reconciled using the same page and exact
receipt. An unknown receipt is not a failure or permission to rerun an action.
Gaps are durable coverage evidence, never invented business candidates.

The additive `zimu_native_ingress_v1` revision follows the pinned 0037 schema.
Validate a consistent SQLite backup and restore before the unique release owner
changes the running version. Retain native tables and receipts when rolling the
application back; downgrade refuses to erase nonempty receipt state. Fence the
legacy source consumer before switching the same source to the native reader.
Never run both consumers or reset an old cursor as if it were a new message.

Before actual enablement, review the precise service credential/network/source
approval and human observer changes. Owner-only files do not isolate credentials
from another process with the same OS identity. Tests do not establish production
custody, actual upstream availability or permitted model sharing.

The candidate must additionally pass the current MCP transport/scope and installed
plugin compatibility checks, same-version backend/frontend browser checks, and
one authorized real message per channel returning to the original OpenAgents
thread. These are separate from source acceptance, Git merge and deployment.

## Reproducible offline evidence

`tests/integration/platform/native_http_support.py` uses real Quart routes,
ApiKeyService authentication, the default NativeAccess policy and scoped SQLite.
Source grants and deployment placement are explicitly synthetic boundaries.
`test_native_host_core_http.py` loads the SHA-verified public source snapshot and
runs the real Host lock, Producer and workflow transactions against that Core
fixture. It covers normal ACK, response loss after commit, restart, unregistered
key refusal and consumer-realm mismatch. It does not claim model execution.

Run candidate code only in the established disposable, no-secret test container;
do not mount an authenticated home, Docker socket, source profiles or production
state. The snapshot manifest records its revision and file hashes. Synthetic UI
browser fixtures establish component behavior only, not a live source deployment.
