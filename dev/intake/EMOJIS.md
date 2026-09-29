# Discord-synchronized Clan Chat emojis

This repository implements a standalone synchronization boundary. It is not
activated by committing these files.

## Why the synchronizer is standalone

Midgard's active Nocturne analytics process is mutable, not a Git checkout, and
combines Discord gateway access with rank synchronization, Group PvM analytics,
member-portal actions, reminders and event notifications. The Cottus Discord
status service is also non-Git and operationally unrelated. The Git-backed Java
leaderboard bot performs leaderboard, spreadsheet and proof-image work and has
no emoji update integration. Adding publication to any of them would couple a
public plugin data path to broader credentials and unrelated failure domains.

`emoji_sync.py` is therefore a repository-owned one-shot process. A hardened
systemd timer runs it at least every five minutes with bounded jitter. It uses
dedicated `LoadCredential` files for the token and strict synchronization
configuration and publishes only normalized public data. A
future gateway integration may trigger immediate runs on Discord's Guild Emojis
Update event, but periodic reconciliation remains authoritative.

## Discord input and eligibility

The only Discord API operation is List Guild Emojis. The synchronizer consumes
only `id`, `name`, `animated`, `available`, `managed`, and `roles`. Creator/user
objects and all unrelated fields are ignored and never logged or published.
Image requests are derived from validated numeric emoji IDs; raw CDN URLs are
never accepted from Discord metadata or exposed publicly.

An emoji is eligible only when:

- its ID is a non-null decimal Discord snowflake;
- its NFKC-normalized lowercase name matches `[a-z0-9_]{1,32}`;
- `available` is true, `managed` is false, and `roles` is empty;
- neither ID nor normalized name is on the bounded server denylist;
- both ID and normalized name are unique within the response;
- its bounded image response has the expected MIME/container, decodes safely,
  fits the encoded/decoded limits, and normalizes successfully.

Conflicting IDs/names are all omitted. Logs contain only bounded result
categories, accepted count and a shortened public revision—not names, images,
headers, identities or credentials. Static PNGs and the first frame of animated
GIFs are rendered onto a transparent 20×20 canvas without upscaling. Animation
is deferred; such entries retain `animated_source=true`. Invalid or unsafe
animated inputs are omitted.

## Atomic mirror and public API

Each successful reconciliation is built in a private temporary directory,
validated, fsynced and renamed into `generations/<revision>`. An atomic `current`
symlink selects the complete generation. Up to three verified generations are
retained. A transport/API/rate-limit failure preserves the selected generation;
a successful Discord response containing no eligible emojis publishes a
distinguishable `ok_empty` generation.

The manifest contains schema version 1, a deterministic SHA-256 content
revision, generation timestamp, bounded source status and sorted public entries.
Entries contain only normalized name, SHA-256, 20×20 dimensions, byte length,
`animated_source`, and a fixed same-origin digest path. Discord IDs are not
needed by the client and are not published.

- `GET/HEAD /api/plugin/v1/emojis` — deterministic JSON, ETag/304, at most 256
  entries, 256 KiB maximum.
- `GET/HEAD /api/plugin/v1/emojis/assets/<sha256>.png` — current-manifest assets
  only, 8 KiB maximum, verified digest, immutable caching.

The intake's read-only bind is `/run/nocturne-plugin-emojis`. It cannot see the
credential, Discord bot state, databases or unrelated paths.

The emoji unit declares the exact nested
`StateDirectory=nocturne-plugin-emojis/public`, so systemd creates the public
leaf with the DynamicUser identity before credentials or network are used. The
synchronizer independently validates that leaf and rejects links, mounts,
foreign ownership, unexpected modes, and extended ACLs. Source credential
files may remain root-owned mode 0600; systemd copies them into its private
per-unit credential directory. Runtime code uses the documented
`${CREDENTIALS_DIRECTORY}` path and does not infer source-file safety from the
mode of systemd's private copy.

## RuneLite behavior

The client polls the manifest about every five minutes with bounded jitter, a
five-second call timeout, ETag/304, disabled redirects/retries, one manifest
request and at most four isolated emoji requests. It accepts assets only from
`https://nocturne.events/api/plugin/v1/emojis/assets/<sha256>.png`, rechecks MIME,
size, dimensions, alpha, PNG structure and SHA-256, then atomically activates a
complete cache generation.

Only `ChatMessageType.CLAN_CHAT` is formatted. The bounded linear parser skips
existing RuneLite tags, recognizes exact lowercase literal tokens, and replaces
at most five using `ChatIconManager`. It does not subscribe to outgoing/input
events or retain message nodes/text. Existing literal text modified by another
formatter is respected. Overlapping triggers are first-formatter-wins. Icon
registrations are capped at 256 for a client session; removed triggers disable
immediately, though their reserved slots and already-rendered historical lines
may remain until restart because RuneLite has no safe ownership-aware rollback.

## Credentials, rights and moderation

Production should use a dedicated least-privilege Discord application permitted
only to read guild emoji metadata. Reusing a broad existing bot token is an
explicit deployment risk decision, never the default. The RuneLite client never
receives a Discord token and never contacts Discord.

Automation does not prove redistribution permission. Only emojis the clan is
permitted to mirror should remain installed/enabled. The server denylist can
suppress an ID or normalized name immediately. Review of the live guild
collection's rights and Plugin Hub suitability is a release gate. Mirror data is
never executable, and images are validated independently on server and client.
Because the eligible collection can change without a plugin-code update,
Plugin Hub reviewers must explicitly confirm that this same-origin,
runtime-downloaded, operator-moderated image model is acceptable. RuneLite's
published rejected-feature list specifically raises redistribution concerns for
third-party emote services; this design does not use those services, but it does
not remove the clan operator's responsibility to prove rights for every mirrored
asset before public distribution.

The eventual Plugin Hub marker must also include an installation `warning=`
field stating that Nocturne automatically requests announcements and emoji
assets from a third-party `nocturne.events` server, that loot/RSN and self-only
CoX presence are sent where applicable, and that these requests expose the
user's IP address. The source metadata and README already disclose this behavior,
but those do not replace the marker-level installation warning required for
automatic third-party traffic.

## Future deployment and rollback boundary

1. On Midgard's CPython 3.14, glibc x86-64 runtime, build a dedicated environment
   with `pip --no-index --require-hashes --only-binary=:all:` and the release's
   `emoji-sync-requirements.txt`. The single mode-0444 wheel lives in the exact
   versioned wheelhouse directory. It admits only Pillow 12.3.0's verified
   `cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64` wheel. Refuse a
   different interpreter, architecture, source distribution or wheel digest;
   never copy a development virtual environment.
2. Create a dedicated Discord application or explicitly approve reuse risk.
3. Store the token at the token `LoadCredential` source path. Create
   `/etc/nocturne-plugin/emoji-sync.json` as a private regular file containing
   only strict JSON such as `{"guild_id":"<numeric id>","denylist":[]}`. Both
   are copied by systemd into the service's private credentials directory; no
   token or guild configuration is passed in an environment variable or process
   argument.
4. Prepare and verify the immutable release plus its commit-scoped four-unit and
   Nginx staging directories.
5. Stop intake, writer, emoji synchronizer and timer, and prevent a concurrent
   Nginx reload. Activate the exact commit with explicit stopped-services
   confirmation, independently captured systemd inactive/PID evidence and the
   exact digest from the read-only activation preflight. Activation changes
   `current`, all four matching units and the
   matching route transactionally, but never reloads or starts anything.
6. `emoji_service_support.py` remains only as a narrow repair helper. It can
   install the emoji service/timer from verified commit staging, never the
   intake unit, and requires the matching applied activation record before it
   proves every artifact outside that narrow repair scope is intact.
   `emoji_route_support.py` has the same activation-record interlock and likewise
   consumes only verified commit-scoped staging. Neither helper installs
   directly from the mutable source tree.
7. Separately approve daemon reload, writer and intake startup, Nginx reload,
   and health checks. Wait boundedly for the writer socket and intake health,
   then start the emoji one-shot and timer. Emoji failure leaves intake
   available and the optional emoji endpoint fails open. Verify the first
   generation before tester rollout.

Activation and the narrow repair helpers preserve metadata/ACLs, create
verified backups, restore automatically on failure, refuse drift and mixed
commits, and offer exact rollback. Unit apply/rollback requires direct systemd
inactivity verification in addition to explicit maintenance confirmation.
