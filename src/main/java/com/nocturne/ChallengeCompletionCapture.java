package com.nocturne;

import java.time.Instant;
import java.util.function.Consumer;
import java.util.function.LongSupplier;

/** Consent gate and user-facing outcome policy, kept separate from parsing and transport.
 * Active published capture policy is enforced by every POST; the v1 server contract has no
 * client policy-read endpoint and never lets cached client state approve an observation.
 */
final class ChallengeCompletionCapture
{
	private final ChallengeCompletionCorrelator correlator;
	private final GenericCompletionCorrelator genericCorrelator = new GenericCompletionCorrelator();
	private final ChallengeObservationService service;
	private final Consumer<String> feedback;
	private final Consumer<String> diagnostic;
	private final LongSupplier nanoTime;

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic)
	{
		this(correlator, service, feedback, diagnostic, System::nanoTime);
	}

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic, LongSupplier nanoTime)
	{
		this.correlator = correlator;
		this.service = service;
		this.feedback = feedback;
		this.diagnostic = diagnostic;
		this.nanoTime = nanoTime;
	}

	void onMessage(String text, RaidCompletionEvidence evidence, String reporter, boolean consent,
		boolean diagnostics, Instant now)
	{
		onMessage("GAMEMESSAGE", text, evidence, reporter, consent, diagnostics, now, 0);
	}

	void onMessage(String category, String text, RaidCompletionEvidence evidence, String reporter, boolean consent,
		boolean diagnostics, Instant now, long gameCycle)
	{
		if (!consent) { clearPartial(); return; }
		ChallengeObservation generic = genericCorrelator.onMessage(category, text, gameCycle,
		nanoTime.getAsLong(), now);
		if (generic != null) submit(generic, diagnostics);
		ChallengeObservation observation = correlator.accept(text, evidence, reporter, now);
		if (observation != null) submit(observation, diagnostics);
	}

	void onBossCompletion(String source, String reporter, String clientSessionId, long gameCycle, boolean consent,
		boolean diagnostics, Instant now)
	{
		if (!consent) { clearPartial(); return; }
		long completionNanos = nanoTime.getAsLong();
		BossCompletionContext context = BossCompletionContext.fromNpcLoot(source, reporter, clientSessionId,
			gameCycle, completionNanos, now);
		ChallengeObservation observation = genericCorrelator.onCompletion(context, completionNanos, now);
		if (observation != null) submit(observation, diagnostics);
	}

	void onSystemMessage(String category, String text, long gameCycle, boolean consent,
		boolean diagnostics, Instant now)
	{
		if (!consent) { clearPartial(); return; }
		ChallengeObservation observation = genericCorrelator.onMessage(category, text, gameCycle,
			nanoTime.getAsLong(), now);
		if (observation != null) submit(observation, diagnostics);
	}

	void onTick(boolean consent, boolean diagnostics, Instant now)
	{
		if (!consent) { clearPartial(); return; }
		ChallengeObservation observation = genericCorrelator.poll(nanoTime.getAsLong(), now);
		if (observation != null) submit(observation, diagnostics);
	}

	void reset() { correlator.reset(); genericCorrelator.reset(); }
	void clearPartial() { correlator.clearPartial(); genericCorrelator.clearPartial(); }
	void onEvidence(RaidCompletionEvidence evidence, String reporter, boolean consent,
		boolean diagnostics, Instant now)
	{
		if (!consent) { clearPartial(); return; }
		ChallengeObservation observation = correlator.onEvidence(evidence, reporter, now);
		if (observation != null) submit(observation, diagnostics);
	}
	void close() { correlator.clear(); genericCorrelator.clear(); service.cancelPending(); }

	private void submit(ChallengeObservation observation, boolean diagnostics)
	{
		String id = service.submit(observation, result ->
		{
			if (result.httpStatus >= 400)
				diagnostic.accept("automatic Challenge observation response http_status=" + result.httpStatus
					+ " state=" + result.state.name().toLowerCase(java.util.Locale.ROOT)
					+ " reason=" + (result.reason == null ? "none" : result.reason));
			switch (result.state)
			{
				case ACCEPTED:
					feedback.accept("Nocturne recorded your " + observation.displayName + " time: "
						+ ChallengeObservation.formatTime(observation.displayDurationMillis) + "."); break;
				case DUPLICATE: feedback.accept("Nocturne already recorded this completion."); break;
				case SERVER_FAILURE: feedback.accept("Nocturne could not confirm this completion. Please try again later."); break;
				case RATE_LIMITED: feedback.accept("Nocturne is receiving reports too quickly. This completion was not confirmed."); break;
				case INVALID: case CONFLICT: case PROTOCOL_ERROR:
					if (diagnostics && result.httpStatus < 400)
						diagnostic.accept("automatic Challenge observation rejected category="
							+ result.state.name().toLowerCase(java.util.Locale.ROOT));
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
