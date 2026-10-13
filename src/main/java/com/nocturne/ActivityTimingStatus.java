package com.nocturne;

/** Display-only timing submission state, kept independent from drop delivery. */
enum ActivityTimingStatus
{
	SENDING("Time · sending"),
	ACCEPTED("Time · recorded"),
	DUPLICATE("Time · already recorded"),
	LOCAL_ONLY("Time · local only"),
	FAILED("Time · not confirmed");

	final String label;

	ActivityTimingStatus(String label) { this.label = label; }
}
