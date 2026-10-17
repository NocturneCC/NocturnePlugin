package com.nocturne;

/** Retries a just-loaded Doom completion header briefly while the client script fills it. */
final class DoomCompletionHeaderCapture
{
	private static final int MAX_RETRY_TICKS = 3;
	private int retriesRemaining;
	private String baseline;
	private boolean pending;

	void arm(String currentText)
	{
		baseline = DoomRunTracker.normalizeCompletionHeader(currentText);
		retriesRemaining = MAX_RETRY_TICKS;
		pending = true;
	}

	Integer poll(String currentText)
	{
		if (!pending) return null;
		String normalized = DoomRunTracker.normalizeCompletionHeader(currentText);
		Integer level = DoomRunTracker.completedLevel(currentText);
		if (level != null && !normalized.equals(baseline))
		{
			clear();
			return level;
		}
		if (--retriesRemaining <= 0) clear();
		return null;
	}

	void clear()
	{
		pending = false;
		baseline = null;
		retriesRemaining = 0;
	}

	boolean isPending() { return pending; }
}
