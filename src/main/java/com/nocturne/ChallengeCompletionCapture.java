package com.nocturne;

import java.time.Instant;
import java.util.function.Consumer;

/** Consent gate and user-facing outcome policy, kept separate from parsing and transport. */
final class ChallengeCompletionCapture
{
	private final ChallengeCompletionCorrelator correlator;
	private final ChallengeObservationService service;
	private final Consumer<String> feedback;
	private final Consumer<String> diagnostic;

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic)
	{
		this.correlator = correlator;
		this.service = service;
		this.feedback = feedback;
		this.diagnostic = diagnostic;
	}

	void onMessage(String text, RaidCompletionEvidence evidence, String reporter, boolean consent,
		boolean diagnostics, Instant now)
	{
		if (!consent) { correlator.clearPartial(); return; }
		ChallengeObservation observation = correlator.accept(text, evidence, reporter, now);
		if (observation != null) submit(observation, diagnostics);
	}

	void reset() { correlator.reset(); }
	void clearPartial() { correlator.clearPartial(); }
	void onEvidence(RaidCompletionEvidence evidence, String reporter, boolean consent,
		boolean diagnostics, Instant now)
	{
		if (!consent) { correlator.clearPartial(); return; }
		ChallengeObservation observation = correlator.onEvidence(evidence, reporter, now);
		if (observation != null) submit(observation, diagnostics);
	}
	void close() { correlator.clear(); service.cancelPending(); }

	private void submit(ChallengeObservation observation, boolean diagnostics)
	{
		String id = service.submit(observation, result ->
		{
			switch (result.state)
			{
				case ACCEPTED:
					feedback.accept("Nocturne recorded your " + observation.displayName + " time: "
						+ ChallengeObservation.formatTime(observation.displayDurationMillis) + "."); break;
				case DUPLICATE: feedback.accept("Nocturne already recorded this completion."); break;
				case SERVER_FAILURE: feedback.accept("Nocturne could not confirm this completion. Please try again later."); break;
				case RATE_LIMITED: feedback.accept("Nocturne is receiving reports too quickly. This completion was not confirmed."); break;
				case INVALID: case CONFLICT: case PROTOCOL_ERROR:
					if (diagnostics) diagnostic.accept("automatic Challenge observation rejected category=" + result.state.name().toLowerCase(java.util.Locale.ROOT));
					break;
				case IGNORED: break;
				default: break;
			}
		});
		if (id == null)
		{
			feedback.accept("Nocturne could not queue this completion. Please try again later.");
			if (diagnostics) diagnostic.accept("automatic Challenge observation skipped category=queue_full");
		}
	}
}
