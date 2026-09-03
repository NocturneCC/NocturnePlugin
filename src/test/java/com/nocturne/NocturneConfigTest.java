package com.nocturne;

import org.junit.Test;
import static org.junit.Assert.*;

public class NocturneConfigTest
{
	@Test public void optionalLootSubmissionAndScreenshotsRemainOffByDefault()
	{
		NocturneConfig config = new NocturneConfig() { };
		assertFalse(config.submitTestDrops());
		assertFalse(config.attachScreenshots());
	}
}
