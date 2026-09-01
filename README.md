# Nocturne

RuneLite companion for Nocturne clan members and community event participants.

## Development preview — 0.1.0

This first build adds a purple **N** sidebar showing the logged-in character and
NPC loot detected by RuneLite's `NpcLootReceived` event. Each card shows the NPC,
character, local time, item names, quantities and item IDs.

- Enable **Nocturne** in RuneLite's plugin settings, then click the purple **N**.
- **Track NPC loot** pauses or resumes recording new drops.
- The latest 50 loot events are kept in memory, newest first. The counter includes
  all recorded events since the history was cleared, including evicted entries.
- Logout, character changes, disabling the plugin and closing the client clear
  history. World hopping on the same character preserves it.
- **Clear local history** resets the displayed history and counter.
- Separate kills with identical drops are retained as separate events.

Nocturne makes no external requests in this version and does not save drops to
disk, register event participants, check membership or award points. This does
not change network behavior of RuneLite itself or other installed plugins.

### Detection scope

Only NPC loot events are handled in this preview. Pets, clues, raid reward
interfaces, chests, collection-log notifications and other special sources need
dedicated coverage and testing in later versions. Inventory movement, trading
and picking up arbitrary ground items are not treated as earned NPC drops.

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

Unit tests check bounded history, preserving repeated drops, character separation
and clearing. They cannot confirm RuneLite detects live game events.

Manual test checklist:

1. Enable Nocturne while logged in. The sidebar should show your current RSN.
2. Defeat a basic NPC that drops loot (for example a cow or goblin). Verify source,
   item names, quantities and IDs appear in one loot card.
3. Repeat a kill. It should create another card, even if the drops are identical.
4. Pause **Track NPC loot** and repeat. No new card should appear; resume afterward.
5. Clear history. The counter should return to zero and the character stay visible.
6. Hop worlds. Existing history should remain for the same character.
7. Log out, then log into another character. Previous character loot should be gone.
8. Disable and re-enable Nocturne. There should be exactly one sidebar icon and
   a fresh history, with the current RSN restored.

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
