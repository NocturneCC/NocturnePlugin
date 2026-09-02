# Derived value catalogue v1

`shared/derived-value-catalogue.json` is the canonical Java/Python catalogue. The
plugin packages that exact file as a resource; intake code loads it from the
repository root. Duplicate rule IDs, duplicate input IDs, invalid output IDs,
version mismatches and invalid component counts fail startup/tests closed.
Each input ID also has a positionally matched canonical dropped-item name. Only
after complete v4 derived-rule validation may the pending writer use that name
when the untradeable input is absent from `Items.db`. In that case the nullable
internal `regular_submissions.item_id` remains NULL, while the OSRS input ID and
finished-output metadata remain distinct. Direct-market and unknown items get
no fallback and still require the ordinary item catalogue.

## Included rules

- Full output: Ultor, Magus, Bellator and Venator vestiges to their matching rings;
  Araxyte fang to Amulet of rancour; Mokhaiotl cloth to Confliction gauntlets.
- Equal share: the three Noxious halberd parts, three Abyssal bludgeon parts and
  three Brimstone ring parts each receive `output_price // 3`; the four Soulreaper
  axe parts each receive `output_price // 4`.
- Full-output rules deliberately do not deduct rings/icons, chromium ingots,
  torture, tormented bracelet, demon tears, skills or other inputs.

Recipe/ID audit sources: RuneLite `gameval.ItemID`; the OSRS Wiki pages for
[Desert Treasure II](https://oldschool.runescape.wiki/w/Update:Desert_Treasure_II_-_The_Fallen_Empire),
[Noxious halberd](https://oldschool.runescape.wiki/w/Noxious_halberd),
[Abyssal bludgeon](https://oldschool.runescape.wiki/w/Abyssal_bludgeon),
[Brimstone ring](https://oldschool.runescape.wiki/w/Brimstone_ring),
[Araxxor](https://oldschool.runescape.wiki/w/Update:Araxxor), and
[Mokhaiotl cloth](https://oldschool.runescape.wiki/w/Mokhaiotl_cloth).

## Additional candidates audited

No additional rule met all constraints.

- Unsired and similar redemption objects: excluded because redemption is random.
- Pets, jars, ornament kits, recolours and cosmetic unlocks: excluded by policy.
- Tokkul, crystal shards, ancient shards, marks, keys, tokens and other currencies:
  excluded because they have multiple/redemption outputs rather than one objective output.
- Hydra tail/Bonecrusher necklace, ancient icon/quartz sceptres, Scurrius spine/bone
  weapons and granite dust/granite cannonballs: excluded because the finished output
  is untradeable.
- Eye of Ayak, Avernic treads, Venator shards, godsword shards, burning claws,
  tormented synapses, zenyte shards, chromium ingots and other ordinary components:
  excluded because the dropped input is itself tradeable and keeps direct RuneLite pricing.
- Untradeable bonds and charged/degraded/ornamented equipment states: excluded because
  they are not qualifying deterministic monster-drop conversions (and may require fees,
  reversible state changes or cosmetic inputs).
- Clue rewards, raid caches and keys: excluded because the redemption output is random.

## Payload compatibility

Payload v4 is used by the updated plugin. Every item has `price_source`; derived
items additionally carry the rule ID, catalogue version, finished output ID/name,
finished output market price and derived unit price. V4 permits an optional v3-style
screenshot. Intake continues accepting unchanged v1, v2 and v3 payloads. Direct
tradeable v4 items use `runelite_market`; unknown or unpriced untradeables use
`unpriced_untradeable` with zero price.

The review integration requires seven nullable columns prepared by
`derived_review_support.py`. Its installer is dry by default and has not been run.
All imported rows remain `pending`; no approval or `rank_totals` write path is added.
