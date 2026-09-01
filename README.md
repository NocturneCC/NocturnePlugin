# Nocturne

RuneLite companion for Nocturne clan members and community event participants.

## Development preview — 0.2.0

The purple **N** sidebar shows the logged-in character, local loot records, and
group-capture evidence. Each loot card freezes the source, RSN, time, item names,
quantities, item IDs and the group observed when the event arrived.

- Enable **Nocturne** in RuneLite's plugin settings, then click the purple **N**.
- **Track loot** pauses or resumes recording new NPC drops and raid rewards.
- The latest 50 loot events are kept in memory, newest first. The counter includes
  all recorded events since the history was cleared, including evicted entries.
- Logout, character changes, disabling the plugin and closing the client clear
  history. World hopping on the same character preserves it.
- **Clear local history** resets the displayed history and counter.
- Separate kills with identical drops are retained as separate events.

Nocturne makes no external requests in this version and does not save drops or
group names to disk, register event participants, check membership or award points. This does
not change network behavior of RuneLite itself or other installed plugins.

### Detection scope

NPC loot and raid reward `LootReceived` events are handled in this preview.
Keep RuneLite's built-in **Loot Tracker** enabled for raid reward events.
Pets, clues, other chests, collection-log notifications and special server-loot
bosses need dedicated coverage and testing in later versions. Inventory movement, trading
and picking up arbitrary ground items are not treated as earned NPC drops.

### Group capture

**Capture groups locally** enables this preview (no network transmission):

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
and plane, up to 100 entries. It is always marked **Nearby observations only**,
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
late capture, solo evidence, retention and repeated raid rewards. They cannot
confirm RuneLite detects live game events or that the sidebar renders correctly.

Manual test checklist:

1. Enable Nocturne while logged in. The sidebar should show your current RSN.
2. Defeat a basic NPC that drops loot (for example a cow or goblin). Verify source,
   item names, quantities and IDs appear in one loot card.
3. Repeat a kill. It should create another card, even if the drops are identical.
4. Pause **Track loot** and repeat. No new card should appear; resume afterward.
5. Clear history. The counter should return to zero and the character stay visible.
6. Hop worlds. Existing history should remain for the same character.
7. Log out, then log into another character. Previous character loot should be gone.
8. Disable and re-enable Nocturne. There should be exactly one sidebar icon and
   a fresh history, with the current RSN restored.

Group capture test:

1. Defeat a basic NPC near another player. The card should list both RSNs as
   observations, with unknown group size. Moving away must not change that card.
2. Keep the plugin enabled before entering a ToB, ToA or CoX raid. Compare the
   live roster with the real group, including yourself and teammates in other rooms.
3. At completion, open the reward chest. Its loot card should keep that run's
   roster. Reopening the same chest should not add an identical reward card.
4. Test a subsequent run with a different group; names from the earlier group
   must not appear. Teammates who died during the first raid should remain on its card.
5. Disable/re-enable group capture mid-raid. The capture must be incomplete even
   if all currently visible party slots match. Test solo and spectator cases too.

## Planned next stages

- Broader drop-source coverage and a separate Midgard development intake.
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
