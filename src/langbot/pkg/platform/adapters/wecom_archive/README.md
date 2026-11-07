# WeCom archive native source candidate

This adapter consumes the existing owner's saved, versioned source API. It does
not replace the Finance SDK, collection process, original storage, parsing
workers, Router, or OpenAgents. It is receive-only: send, reply, and arbitrary
platform API calls reject requests.

## Admission and recovery

The host must attach the scoped `NativeIngressStore` and a current authority
callback before startup. The adapter reads from the store's committed source
checkpoint, validates the complete page and its authority before and after the
read, and passes the entire page to `accept_page`. Only its durable receipt
advances the checkpoint. An incomplete page, changed grant, malformed revision,
or failed read leaves the checkpoint unchanged. Restarted collection uses the
same scoped store; native record/version identity prevents duplicate admission.
There is no second legacy pipeline emission.

All revisions, including baseline, pending media, and recall, reach the shared
Router feed. `dispatch_eligible` is a local admission annotation and never
filters source-state handoff. The shared Core owns Router cursor/receipt and
single-consumer cutover. Legacy direct source polling must not run concurrently
with the new path at cutover.

Only narrow change metadata, identities, original references, attachment
hashes/status and materialization metadata are persisted. Body, raw payload,
source filesystem paths and secrets are excluded. This candidate does not
assert that metadata visibility is already authorized for any deployment.
LangBot and OpenAgents audiences must have separately verified authority;
matching a configured workspace string is insufficient. Missing host authority
fails closed.

## Original reads

`reader.py` exposes owner-authenticated original and document reads for explicit
current agent grants. `documents.py` validates full manifests and chunk hashes,
UTF-8 boundaries, version stability and current authority across chunks. Partial
windows remain incomplete; they do not certify complete coverage. A shared
absolute read budget bounds multi-request operations. No proxy, redirect,
ambient credentials or automatic retry is used. These APIs are not exposed as
chat tools by this adapter.

The bounded reader/document/transport logic is adapted from the frozen source
contract in `jry21223/zimu-mono`, commit
`4b6643153ff4dec8cda83957a50c317c3857c1dc`, files
`ops/enterprise_agent/source_http.py`, `source_documents.py`, and
`_http_budget.py`. That source candidate and its parsing dependencies need
independent deployment verification; their presence here does not prove a
running owner endpoint or parser.

## Remaining activation gates

Bot configuration accepts only a credential reference and non-secret source binding.
A trusted service factory must inject its scoped source client; endpoints, raw
tokens and source principals are rejected in Bot configuration. This does not
create or install a service-side credential registry entry. Its effective owner
grant must be metadata-only; holding a broader token cannot be described as
removing original-read access.

The native reference-only manifest uses the existing component discovery and
configuration form. It contains no endpoint, raw credential or secret path.
Actual service registration, mounting, grants and audience approval remain
separate activation gates.
No source credential, grant, audience, real account, or production configuration
is created by this code. Runtime host injection and the single Router handoff
must be integrated and verified before enabling a source. Real text, image,
voice, file, recall, revocation and recovery tests remain deployment acceptance,
not claims made by the isolated fixtures.

The targeted tests use a loopback synthetic owner and the real LangBot SQLite
store inside the shared no-network, non-root, no-host-mount runner. They do not
contact an existing account or read a real original.

## Coverage and owner identity proof

Until the shared feed supports typed source gaps, a page with any nonempty gap,
missing gap list, or coverage other than `success`/`success_zero` is rejected.
This deliberately stops intake instead of advancing a cursor while silently
losing coverage evidence.

The fresh `/source-binding` candidate contract has exactly `source`, `scope`,
`principal`, `workspace`, `revision`, `corp_id`, and
`origin=owner_source_binding`. Its source/audience/revision must match current
protected authority reads before and after the proof. The account fact comes
only from the owner-set `corp_id`; a configured account is never substituted.
This proves an owner binding, not an SDK login. An owner without this endpoint
fails closed. Endpoint presence and rollout remain unverified here.

The source-specific `native_validate_change` hook validates exact narrowed
metadata before durable acceptance. The initial narrow projection deliberately
omits original bodies and owner filesystem paths. Its retained
`projection_hash` remains owner provenance; this adapter does not claim to
recompute the owner's projection hash over the new audience's narrowed shape.

## Operator setup and status

Create the native channel with its source/account/scope/grant revision and an
opaque `credential_ref`. The reference must first identify a disabled,
operator-controlled service-registry entry for this exact Bot, Workspace and
binding. Creating or editing Bot configuration grants no source access and does
not provision a credential. Actual service registration, secret mounting,
metadata-only source permissions and audience enabling require their own exact
review. Never paste a source credential or filesystem path into the form.

| State | Meaning | Required operator action |
| --- | --- | --- |
| Inactive | The Bot is disabled; no source connection should run. | Leave disabled until registration and acceptance gates are complete. |
| Missing registration/client | The source grant, registry reference or trusted service client is absent. | Register the exact approved binding through the controlled service path; entering another Bot config value is insufficient. |
| Proof mismatch | Fresh owner proof or its source, corporation, scope, revision or Workspace does not match the registered binding. | Verify the owner's current proof and the intended audience. Do not substitute configured account labels for proof or silently widen the grant. |
| Read only / unsendable | This source intentionally advertises no send API. | Use its archive intake and existing authorized Router path; sending, replying and arbitrary platform calls remain unsupported. |

Status logs describe startup, durable page acceptance and sanitized failure
codes. A startup log alone does not certify connectivity or successful intake.
A durable acceptance receipt certifies local metadata admission; it does not
certify Router execution, original read access or a completed business result.

## Candidate validation record

The earlier composed candidate passed 55 isolated contracts before the final
reference-only configuration change. The subsequent native discovery,
capability and pure configuration-validation slice passed six tests. These are
separate receipts, not an additive total or a production acceptance claim.

Three image/voice/file integration regressions currently reproduce a known
shared-Core failure: valid nonempty six-field `media_evidence` arrays are
rejected by the Core metadata guard. The source adapter does not suppress those
arrays to make admission appear successful. After the shared fix, rerun the
composed source/SQLite/Core contracts, the real HTTP create/update secret
rejection tests, and the coverage-gap handoff tests before declaring a
switchable candidate. Pure constructor validation does not prove that the HTTP
configuration API rejected a secret before persistence.

No test here demonstrates a deployed service, current real-account grant,
working parsing worker, or authorized end-to-end business delivery.
