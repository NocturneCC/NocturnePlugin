package com.nocturne;

import org.junit.Test;
import static org.junit.Assert.*;

public class NocturneConfigTest
{
	@Test public void optionalSubmissionAndScreenshotsRemainOffByDefault()
	{
		NocturneConfig config = new NocturneConfig() { };
		assertFalse(config.showDiagnostics());
		assertFalse(config.submitTestDrops());
		assertFalse(config.submitChallengeTimes());
		assertFalse(config.attachScreenshots());
	}
}
