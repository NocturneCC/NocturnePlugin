# Nocturne

RuneLite companion for Nocturne clan members and community event participants.

## Development preview — 0.3.2

The purple **N** sidebar shows compact loot cards: RuneLite item sprites,
quantities, source, time and delivery status. Raid cards also show a locally
captured roster (marked incomplete where applicable). Random nearby player names,
item IDs and capture internals are hidden by default; **Show capture diagnostics**
reveals them for local testing. Names are not presented as verified clan members.

- Enable **Nocturne** in RuneLite's plugin settings, then click the purple **N**.
- **Track loot** pauses or resumes recording new NPC drops and raid rewards.
- Loot history is stored per normalized RSN under
  `.runelite/nocturne/loot-history/`. The newest 50 events load first; use
  **Load 50 older events** for bounded, newest-first pagination. Logout keeps the
  last character visible, and switching back to a character restores its history.
- The counter and approximate local file usage include the selected character's
  complete history. Records are not automatically removed by age or count.
- **Clear local history** requires confirmation and clears only the selected RSN.
  With RuneLite closed, manual cleanup can remove individual `.jsonl` account
  files or the whole `loot-history` directory.
- Separate kills with identical drops are retained as separate events.

History files contain the card's RSN, source, time, item IDs, names, quantities,
captured unit-price/derived-value metadata, group snapshot and intake outcome.
They never contain screenshots or screenshot bytes. Records are local and
display-only: loading them cannot schedule a screenshot or replay a submission.
JSON-lines overhead varies with item count; a typical one-item event is roughly
0.5–1 KiB, so 10,000 simple events are approximately 5–10 MiB. Writes are forced
and atomically replaced where rewriting is required. Versioned readers preserve
valid records when malformed lines or interrupted temporary files are present.

### Optional test submissions

**Send drops to test intake** is off by default. When enabled, new drops send
only your RSN, source, item IDs, quantities, RuneLite unit prices, timestamp and a random event UUID to
`https://nocturne.events/api/plugin/dev/drops`. During Chambers of Xeric and
Challenge Mode, enabled loot tracking also sends automatic, bounded self-only
presence check-ins to `https://nocturne.events/api/plugin/dev/raid-presence`.
These include your RSN, world, raid timing/group signals and your own final raid
points; they never include another player's name. The server also receives your IP
address as part of the connection. No group names, credentials, membership data
or points are sent. Loot tracking and presence start automatically when Nocturne
is enabled; disable **Track loot** to stop both local tracking and presence network
requests. **Attach drop screenshots** is a separate opt-in setting for
likely point-eligible loot. It sends one compressed RuneLite-canvas JPEG; chat is
excluded by default unless **Include chat in screenshots** is separately enabled.
The cropped game view can still contain visible players, overhead names and
plugin overlays. Full-canvas images can contain public, clan or private chat, so
both screenshot settings are off by default and display RuneLite's external-server warning.
The price is client-reported and does
not prove a drop; Midgard remains responsible for scoring. Item sprites come from RuneLite's
item cache; no image downloads are needed.

The companion test service is in [dev/intake](dev/intake/README.md). Committing
these files does not deploy it. Keep submissions off until the Midgard service
and HTTPS route are installed and checked. It stores unverified test records in
its own capped database and accepts only explicitly configured test RSNs. A
separate local writer can match an active member and create an unverified pending
review row, but cannot approve it or change rank totals. Screenshot bytes are not
stored in the public intake database and are served only through the authenticated
super-admin API. Private storage is capped at 2,000 images; when full, the pending
submission is still created without an attachment.

Cards distinguish **Captured locally**, **Sending to test intake…**, **Received
by test intake**, rejection, full queue, cancellation and unconfirmed delivery.
Only a matching JSON receipt after server storage produces confirmation; a plain
HTTP 200 page is insufficient. There are at most eight pending requests, a
12-second timeout, no redirects, no automatic retries and no disk queue. Turning
submissions off, logout or plugin shutdown cancels pending requests. Cancellation
cannot undo a write already made by the server. Clearing local history does not
delete records from the separate test database. Old drops are not sent when the
toggle is enabled.

Group data remains local while we resolve RuneLite's restriction on crowdsourcing
other players' names. See the official
[restricted feature list](https://github.com/runelite/runelite/wiki/Rejected-or-Rolled-Back-Features).
The backend rejects extra group fields. Group bonuses remain a future feature;
this preview cannot establish group membership or entitlement.

### Detection scope

NPC loot and raid reward `LootReceived` events are handled in this preview.
Keep RuneLite's built-in **Loot Tracker** enabled for raid reward events.
Pets, clues, other chests, collection-log notifications and special server-loot
bosses need dedicated coverage and testing in later versions. Inventory movement, trading
and picking up arbitrary ground items are not treated as earned NPC drops.

### Group capture

**Capture groups locally** enables collection (group names are never transmitted):

- ToB/HMT and ToA: read the game's party-name string slots.
- CoX/CM: read the game's raiding-party sidebar names.
- Names are accumulated during the run, including the local player. They are
  frozen on completion/exit and retained for about ten minutes for reward chests.
- Team size comes from the peak occupied party slots (ToB/ToA) or the CoX party
  size varbit. For ToB/ToA this is an observed size signal, not a guaranteed
  independent complete-player count: deaths or missed samples can affect it.
- **Roster matches observed size** means entry was observed, your name appeared
  in the roster, and the unique name count matches that size signal. It does not
  mean anyone's membership, contribution or eligibility has been verified.
- Late enables, spectators, unknown counts, missing recipient names and count
  mismatches remain **Capture incomplete**. Empty capture is never assumed solo.
- Character changes, world hops, and toggling group capture reset live rosters.
  Starting another raid replaces the previous run. Old loot cards remain frozen.
- With a retained run context, repeated identical raid reward bundles are
  suppressed. This in-memory guard cannot identify a reopened chest after the
  plugin/session history was cleared or expired.

For non-raid NPC drops (including Nex), a one-time **20-tile proximity snapshot**
includes the local character and nearby visible names in the same world view
and plane, up to 100 entries. With diagnostics enabled, it is marked **Nearby observations only**,
with unknown team size. It is not a room roster or proof of participation. It
does not retain people who left before the drop. Full Nex encounter tracking is
a separate next stage; this preview must not be used to award Nex group bonuses.
Nearby capture is disabled in the Wilderness and PvP/Deadman/PvP Arena worlds.

No ambient proximity scans run each tick. Raid roster reads are sampled every
five game ticks; nearby names are read only when a non-raid NPC loot event arrives.

## Run locally on Windows

Use IntelliJ IDEA, Git and Eclipse Temurin JDK 11. Set IntelliJ's **Gradle JVM**
to Java 11 and use the included Gradle wrapper.

From the project directory in IntelliJ's terminal:

```powershell
.\gradlew.bat run
```

The development launcher loads Nocturne automatically. Search for **Nocturne**
in the plugin list and enable it if necessary.

Jagex Account users should follow RuneLite's official
[development login guide](https://github.com/runelite/runelite/wiki/Using-Jagex-Accounts).
Keep `credentials.properties` private and outside this repository. Never upload
it, include it in logs, or share it for debugging.

### Fetch the development branch

Close the development client before updating. If Git reports local changes,
inspect and preserve them before continuing; do not reset them blindly.

```powershell
git fetch origin
git switch development
git pull --ff-only
.\gradlew.bat run
```

## Checks

```powershell
.\gradlew.bat clean test
```

Unit tests check bounded history, repeated drops, frozen rosters, missing names,
late capture, solo evidence, retention, repeated raid rewards, captured prices, payload privacy and
response acknowledgement. The isolated intake has its own Python tests. They cannot
confirm RuneLite detects live game events or that the sidebar renders correctly.

Manual test checklist:

1. Enable Nocturne while logged in. The sidebar should show your current RSN.
2. Defeat a basic NPC that drops loot (for example a cow or goblin). Verify source,
   item sprites, names and quantities appear in one loot card. It should say
   Captured locally while submissions are off. Item IDs require diagnostics.
3. Repeat a kill. It should create another card, even if the drops are identical.
4. Pause **Track loot** and repeat. No new card should appear; resume afterward.
5. Clear history. The counter should return to zero and the character stay visible.
6. Hop worlds. Existing history should remain for the same character.
7. Log out, then log into another character. The first character's history must
   remain stored; switching back must restore it.
8. Disable and re-enable Nocturne. There should be exactly one sidebar icon and
   the current RSN and its existing history restored.

Group capture test:

1. Enable **Show capture diagnostics**, then defeat a basic NPC near another
   player. The card should list both RSNs as observations, with unknown group size. Moving away must not change that card.
2. Keep the plugin enabled before entering a ToB, ToA or CoX raid. Compare the
   live diagnostic roster with the real group, including yourself and teammates in other rooms.
3. At completion, open the reward chest. Its loot card should keep that run's
   roster. Reopening the same chest should not add an identical reward card.
4. Test a subsequent run with a different group; names from the earlier group
   must not appear. Teammates who died during the first raid should remain on its card.
5. Disable/re-enable group capture mid-raid. The capture must be incomplete even
   if all currently visible party slots match. Test solo and spectator cases too.

Screenshot attachment test:

1. Leave **Attach drop screenshots** off and confirm an eligible pending drop uses
   the item-icon fallback on the review page.
2. Enable **Attach drop screenshots**, leave chat inclusion off, and receive loot
   containing an individual item worth at least 500,000 gp. Confirm the pending
   review card shows the captured game viewport and no chat box.
3. Separately enable **Include chat in screenshots** only after reviewing its
   warning. On another eligible drop, confirm the full canvas is attached.
4. Confirm excluded low-value loot never creates an image file and approving or
   denying a pending item still leaves rank totals governed by the existing review flow.

## Planned next stages

- Deploy and verify the prepared Midgard development intake; broaden drop coverage.
- Backend member/alt RSN lookup and event-only guest enrollment, without Discord login.
- Custom chat emojis and clearly labeled Nocturne announcements.

An RSN lookup establishes eligibility, not proof that the sender owns that
character or obtained a drop. Client-supplied reports can be forged; timestamps,
item validation and deduplication do not turn them into verified evidence.
The scoring and review policy must account for this before automatic awards.

## Publishing

This is a development project, not a Plugin Hub submission. Develop and test here
before creating a separate Plugin Hub marker PR. The project uses the official
[RuneLite example-plugin](https://github.com/runelite/example-plugin) Gradle
structure and targets Java 11 with `build=standard`.

License: BSD-2-Clause; see [LICENSE](LICENSE).
Adapted raid-source mapping is attributed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
