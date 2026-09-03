package com.nocturne;

import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class InstanceObservedEvidenceTest
{
	@Test public void evidenceIsEpochScopedAndTracksFirstLastAndNearCompletion()
	{
		InstanceObservedEvidence evidence = new InstanceObservedEvidence();
		evidence.begin(7, List.of("Local", "One"), 10);
		evidence.observe(7, List.of("One", "Two"), 20);
		evidence.observe(6, List.of("Stale"), 21);
		evidence.complete(7, 65);
		InstanceObservedEvidence.Snapshot snapshot = evidence.snapshot();
		assertEquals(3, snapshot.players.size());
		assertEquals(2, snapshot.nearCompletionCount());
		assertEquals(10, snapshot.players.get(1).firstSeenTick);
		assertEquals(20, snapshot.players.get(1).lastSeenTick);
		assertFalse(snapshot.accepting);
	}

	@Test public void leavingRaidStopsObservationAndNextEpochClearsPriorRaid()
	{
		InstanceObservedEvidence evidence = new InstanceObservedEvidence();
		evidence.begin(1, List.of("Old"), 1);
		evidence.stop(1);
		evidence.observe(1, List.of("Reward Area"), 2);
		assertEquals(1, evidence.snapshot().players.size());
		evidence.begin(2, List.of("New"), 3);
		assertEquals(List.of("New"), evidence.snapshot().players.stream().map(seen -> seen.name)
			.collect(java.util.stream.Collectors.toList()));
	}
}
