package com.nocturne;

import org.junit.Test;
import static org.junit.Assert.*;

public class GauntletCompletionSignalTest
{
	@Test public void completionSignalRequiresActiveGauntletAndEligibleAnchoredSystemMessage()
	{
		String message = "<col=ff9040>Corrupted   challenge duration:</col> 9:12. Personal best: 7:15.";
		assertTrue(GauntletCompletionSignal.isCompletion("GAMEMESSAGE", message, true));
		assertTrue(GauntletCompletionSignal.isCompletion("ENGINE", message, true));
		assertFalse(GauntletCompletionSignal.isCompletion("GAMEMESSAGE", message, false));
		assertFalse(GauntletCompletionSignal.isCompletion("CLAN_CHAT", message, true));
		assertFalse(GauntletCompletionSignal.isCompletion("GAMEMESSAGE",
			"The Corrupted challenge duration: 9:12", true));
		assertFalse(GauntletCompletionSignal.isCompletion("GAMEMESSAGE",
			"Someone says Corrupted challenge duration: 9:12", true));
	}
}
