# Native personal WeChat metadata ingress

The production candidate uses `OwnerNativeClient` and `OwnerNativeSource` against
the existing thread-scoped source owner. The account credential, collector,
Journal, original reader, media bytes, derived store and sender remain with that
owner. LangBot holds only a separately approved metadata-reader reference. No
credential, endpoint, listener, network route or source permission is installed
by these files.

The existing Bot form discovers `sources/agent_wechat.yaml`. Choose the explicit
`personal-wechat-native-v1` flavor, existing source/scope/account/thread,
credential reference and approved owner cursor. The endpoint and narrow reader
principal/profile revisions are controlled by the service registry. Bot config
cannot contain raw tokens, secret paths or endpoint overrides. Native config
also refuses collector `consumer_id`, `activation_at` and send enabling.
A conversation list is a filter, never an authorization grant.

The fixed native client calls only `GET /native/binding`, `GET /native/authority`,
`GET /native/changes` and `GET /native/descriptor`, using the registered `X-Operator`, narrow Bearer reader,
fixed source/scope/profile audience and `stage=ingest`. It checks the actual
current grant and binding revisions before and after reads. The descriptor contains only exact reference/version, projection hash, change
kind and original-availability metadata. It has no original body, media, derived,
materialization or send methods. The OA reader uses the original
source directly with its separately registered dispatch/result/display purpose
and actual agent; metadata credentials do not inherit that access.

Owner canonical change/record/version/reference/projection hash are preserved.
The owner version binds input identity, representation, parser pin and derived
result hash. Completion or reparse is a new owner version and append-only frame;
LangBot never computes a shadow version. Legacy text references remain an
explicit separate flavor; installation does not migrate old identities, Journal
rows, cursors or pending history.

Three sequence domains stay separate: the owner's signed/opaque page cursor,
the raw wx event cursor, and Core's delivery position. Native normal envelopes
explicitly carry `owner_frame_sequence`; typed unresolved frames carry the same
owner sequence without inventing a message record/reference. Core persists the
owner-domain sequence for restart. Finance per-record revisions are unrelated.
Normal and unresolved events share the bounded 100-frame page limit; owner
coverage gaps are separate. Core commits the metadata and next owner cursor in
one transaction. This saved source feed has no external consumer ACK API and
never changes the wx collection watermark.

Runtime state distinguishes source feed availability from upstream connection.
Starting a task or successfully reading HTTP is insufficient for `connected`.
Only a completed valid metadata poll plus a fresh exact-account collector proof
(authenticated, online, native push, sync ready, timezone-aware observation)
can show connected. Missing, stale, malformed or simulator evidence stays
unknown; an explicitly disconnected collector waits upstream. Error output is
limited to static safe codes. The original offline account and its pending
projection batches are not modified by this adapter.

`client.py`, `projection.py`, `source.py` and `sender.py` retain the earlier
**offline direct-wx contract** tests. wx v1.4 account read/media tokens span all
threads; these are not registered production transports. Their text/media
integrity tests and 32 MiB bounded read helper do not establish production media
parsing, body-reader access or permission to move an account token into LangBot.

Verification runs in the shared pinned LangBot 4.11.0 isolated runner, without
credentials, host mounts, external network or production processes. Synthetic
HTTP plus real Core/SQLite tests are implementation evidence, not installation,
real group reception, single-consumer cutover, UI browser or Router/OA acceptance.
