# Pinned wx v1.4.0 wire facts

This is a source audit, not a server probe. All links below use the fixed
`thisnick/agent-wechat` `v1.4.0` tag. DTOs and profile rows are different layers.
The production adapter consumes the scoped owner's canonical changes, not this
raw account API.

## Primary call chain

- [`wx/src/main.rs`, 1833–1843](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wx/src/main.rs#L1833): `messages get` requests the account-scoped `messages/{id}` REST resource and checks a supplied thread against the response.
- [`wechat-api/src/lib.rs`, 869–880](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-api/src/lib.rs#L869): the handler requires account `read`, calls Core, and validates the returned value as `NativeMessage`.
- [`wechat-api/src/contracts.rs`, 18–47](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-api/src/contracts.rs#L18): the public wire DTO declares the fields below, including a required string `version`.
- [`wechat-core/src/profile.rs`, 252–260](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-core/src/profile.rs#L252): Core reads an exact profile message and applies recall normalization.
- [`wechat-core/src/profile.rs`, 291–375](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-core/src/profile.rs#L291): normalization builds the public JSON.
- [`wechat-profile/src/model.rs`, 49–63](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-profile/src/model.rs#L49): the lower-level profile row has different names and no `version` field.

## Raw field mapping

| Wire field | Actual v1.4 source and limitations |
| --- | --- |
| `id` | Required string. Core `message_id(SourceMessageId)` encodes the exact profile identity; it is not a synthesized LangBot record. |
| `accountId` | Required string from the account selected by the authenticated REST path. The profile-row model does not contain this wire field. |
| `threadId` | Required string in DTO; normalization uses the row's optional peer. A missing peer cannot be filled from a requested group to manufacture evidence. Recall normalization can restore it from a bound marker. |
| `type` | Wire i32 message type from the profile message type. Recalled originals are normalized to system type 10000. Type alone does not establish recall. |
| `body` | Nullable string. `normalized_message` selects decoded text and supported representations; non-text often has no body. There is no public Message `text` field. A filename, XML or label is not derived document text. |
| `serverId` | Nullable string from the server message ID. Preserve the string; no float or guessed zero. |
| `senderId` | Nullable string. Ordinary messages use the profile sender; system notices deliberately have no asserted author. Recall attribution is optional and may remain absent. |
| `direction` | Nullable string derived from the optional outgoing flag. System notices and unproven recall attribution may have no direction. Do not require `incoming` before applying an already-established recall tombstone. |
| `createdAt` | Nullable RFC3339 value derived from the profile's Unix-second creation time. It is distinct from observation/event time; absent time is not an activation proof. |
| `version` | Required wire string supplied as `reader.generation()`, not a per-message semantic revision. The lower-level profile `Message` has no such field. |
| `recall` | Optional DTO object with observed time and optional recall time/replacement notice. The REST single-message path merges applicable local recall records and official profile evidence. Absence means no applicable marker was emitted by that snapshot, not proof that no remote/uncollected recall exists. |
| `media` | DTO array initialized empty by normalization. Obtain bound assets from `messages/{id}/media`; do not infer full attachment evidence from the Message array alone. |

`accountId`, `body`, `createdAt`, `direction` and `version` above are official REST
normalization fields, not fields of the raw profile-row struct. Owner canonical
`record`, `input_hash`, representation/parser/result hashes and `owner_version`
are separately derived by the existing scoped Journal; they are not wx DTO
fields. Synthetic `native-version-1` fixtures test transport shape only.

## Generation is a snapshot fence, not message identity

[`wechat-profile/src/reader.rs`, 294–296](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-profile/src/reader.rs#L294)
returns the snapshot generation. For an owned live profile,
[`owned_reader.rs`, 139–156](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-profile/src/owned_reader.rs#L139)
walks the profile storage and begins a hash using its root path.
[`owned_reader.rs`, 168–197](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-profile/src/owned_reader.rs#L168)
adds main/WAL file size and modification/creation stamps; SHM is explicitly
excluded from settled-content identity. Finally,
[`owned_reader.rs`, 298–305](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-profile/src/owned_reader.rs#L298)
combines relative paths and stamps. This spans the profile, not one message.
Unrelated message/contact writes, profile movement or file-stamp changes can
therefore change the generation without changing the target message. It must
not enter semantic owner identity or invalidate an unchanged old reference.

Preserve the actual generation separately as observation/snapshot provenance
when available. A transport that genuinely omits it records `None`; it must not
invent a native revision from a timestamp, random ID or local content hash.
Semantic owner input should bind the exact message identity and relevant actual
fields/body, durable recall evidence, sorted bound media, and representation /
parser / result evidence. Version changes then follow those semantic inputs,
not unrelated profile activity. The earlier direct offline projector hashes
the global string and is not the production identity algorithm.

## Recall evidence and updates

[`wechat-core/src/recalls.rs`, 477–544](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-core/src/recalls.rs#L477)
checks exact thread/server and source identity against the account's durable
recall records, and can observe official profile recall evidence.
[`recalls.rs`, 694–780](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-core/src/recalls.rs#L694)
passes the reader generation to normalization and, for an applicable recall,
clears the original body/media/quote representation and adds the recall object.
[`wechat-store/src/recalls.rs`, 78–106](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-store/src/recalls.rs#L78)
shows account-local exact marker and source-identity lookup.

The official code also deliberately stops applying a completed marker when an
official row has been restored (`recalls.rs`, 487–491). Consequently the scoped
owner's stronger non-resurrection policy must retain its own already-confirmed
tombstone and merge that evidence. A missing JSON field, changed generation,
normal-looking body or absent sender/direction cannot undo such a tombstone.
Fresh message GET is local normalized evidence, not a remote no-recall promise.

A second volatility detail matters: `recalls.rs`, 520–533 can construct an
in-memory marker with the current UTC observation time during a read snapshot.
Consequently a fresh `recall.observedAt` is not necessarily a stable source
revision. The owner must bind stable recall state/target and its durable
confirmed ledger, retain first/actual observation evidence, and keep repeated
read-observation clocks out of semantic version hashes.

## Exact cached media reads

[`wechat-api/src/media.rs`, 198–213 and 287–345](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-api/src/media.rs#L198)
requires the account's media permission for asset metadata and cached bytes.
The actual routes are `messages/{id}/media`, `media/{asset_id}` and
`media/{asset_id}/content`. Bind account/message/asset before reading, require
available cached state and verify actual size/SHA-256/MIME.
[`wechat-core/src/media.rs`, 371–416](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wechat-core/src/media.rs#L371)
authorizes the record before/after cache access and verifies decrypted payload
integrity. Account media permission is still not thread-scoped permission.

There is no need to guess a `read-asset` CLI. The actual existing CLI supports
`wx --json --account <account> messages media <thread> <message> --cached-only`
and optional `--output <private-path>` / `--quality <variant>`.
[`wx/src/client_actions.rs`, 781–873](https://github.com/thisnick/agent-wechat/blob/v1.4.0/packages/native-client-rust/crates/wx/src/client_actions.rs#L781)
checks the message thread, selects an available cached asset, returns
`not_cached` under cached-only instead of starting a transfer, and verifies
written bytes. It selects a variant rather than accepting an exact asset ID;
callers needing a specific asset must compare the returned ID and reject a
mismatch. Without cached-only it may create a transfer job. This note does not
authorize starting downloads or writing outside an approved temporary workspace.
