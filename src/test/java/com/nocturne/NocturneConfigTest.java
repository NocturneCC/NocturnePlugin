package com.nocturne;

import org.junit.Test;
import static org.junit.Assert.*;

public class NocturneConfigTest
{
	@Test public void lootAndPresenceTrackingOperateAutomaticallyByDefault()
	{
		NocturneConfig config = new NocturneConfig() { };
		assertTrue(config.trackNpcLoot());
		assertFalse(config.submitTestDrops());
		assertFalse(config.attachScreenshots());
	}
}
