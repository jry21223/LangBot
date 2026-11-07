# Native Agent WeChat candidate

This adapter uses the existing wx v1.4 daemon. It never logs in, starts/stops
an account, creates credentials, downloads unapproved remote media, or emits a
second pipeline event. Core owns durable Router handoff.

The existing Bot configuration UI loads `sources/agent_wechat.yaml`. It contains
an existing credential reference, never a token. Trusted runtime factories must
supply the client, current source/receiver grant, and (separately) any sender
grant. A configured conversation list is only a filter. Missing factories fail
closed. Sending is off by default; legacy SDK send/reply cannot bypass Host
operation IDs or current authorization. Text-only sending has stable operation
keys and lookup; `needs_review` remains review, and an unknown result never
causes blind resubmission. Accepted means daemon intent accepted, not delivered.

`activation_at`, `initial_cursor`, scope and account are revision-bound. Existing
source checkpoints survive restart. Pre-activation catch-up remains a baseline;
post-activation recovery can dispatch only with current permission. wx v1.4
journal cursors are canonical positive signed-i64 decimal strings; raw strings
are retained and future opaque formats are rejected, not guessed.

Only metadata enters Core. Native references are
`wechat-native:message/<record-sha256>/<version-sha256>`. Canonical JSON uses
sorted keys, UTF-8, compact separators, no nonfinite numbers and no final newline.
Original text/UTF-8 chunks are read fresh and checked before and after access.
Media manifests return `kind=media`, the exact native version, and only the
accepted asset ID/kind/state/SHA-256/byte length/MIME list. Text manifests use
`kind=text` with the text SHA-256, byte length and ordered chunk count. Neither
manifest exposes arbitrary source URLs or filenames. The bounded media read limit is 32 MiB; larger assets return a size error, not
a successful parse. Media reads require the exact accepted asset plus current account/message,
version, recall, SHA-256, byte length and MIME checks. No filename or XML is
pretended to be extracted document text. Derived media parsing must use the
existing parser owner and its approved runtime.

Unknown media/recall identities are persisted as internal `unresolved` metadata,
then the source cursor may advance. They are not business dispatches or Host
successes. Core must expose them as gaps. Unknown assets are not fetched. Only
accepted exact pending-message IDs may be refreshed (at most 100 per event).
Unresolved entries without a later exact match require explicit reconciliation;
there is no speculative conversation association or background resend.

Offline verification uses the shared pinned isolated LangBot 4.11.0 runner,
with no host mounts, credentials, external network or production processes.
Offline passing results do not prove native UI, installed factories, the actual
account, single-consumer cutover or Router/OpenAgents production acceptance.
The original disconnected account and its pending projection batches must be
preserved until the authorized owner restores it and performs controlled cutover.
