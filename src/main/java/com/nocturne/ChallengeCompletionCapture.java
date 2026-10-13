package com.nocturne;

import java.time.Instant;
import java.util.function.Consumer;
import java.util.function.BiConsumer;
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
	private final Consumer<ChallengeObservation> accountUnlinked;
	private final BiConsumer<ChallengeObservation, ActivityTimingStatus> activityTiming;
	private final Consumer<BossCompletionContext> activityContextStarted;
	private final LongSupplier nanoTime;

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic)
	{
		this(correlator, service, feedback, diagnostic, System::nanoTime, observation -> { });
	}

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic, LongSupplier nanoTime)
	{
		this(correlator, service, feedback, diagnostic, nanoTime, observation -> { });
	}

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic, Consumer<ChallengeObservation> accountUnlinked)
	{
		this(correlator, service, feedback, diagnostic, System::nanoTime, accountUnlinked);
	}

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic,
		Consumer<ChallengeObservation> accountUnlinked,
		BiConsumer<ChallengeObservation, ActivityTimingStatus> activityTiming)
	{
		this(correlator, service, feedback, diagnostic, System::nanoTime, accountUnlinked, activityTiming, context -> { });
	}

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic, LongSupplier nanoTime,
		Consumer<ChallengeObservation> accountUnlinked)
	{
		this(correlator, service, feedback, diagnostic, nanoTime, accountUnlinked,
			(observation, status) -> { }, context -> { });
	}

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic, LongSupplier nanoTime,
		Consumer<ChallengeObservation> accountUnlinked,
		BiConsumer<ChallengeObservation, ActivityTimingStatus> activityTiming)
	{
		this(correlator, service, feedback, diagnostic, nanoTime, accountUnlinked, activityTiming, context -> { });
	}

	ChallengeCompletionCapture(ChallengeCompletionCorrelator correlator, ChallengeObservationService service,
		Consumer<String> feedback, Consumer<String> diagnostic, LongSupplier nanoTime,
		Consumer<ChallengeObservation> accountUnlinked,
		BiConsumer<ChallengeObservation, ActivityTimingStatus> activityTiming,
		Consumer<BossCompletionContext> activityContextStarted)
	{
		this.correlator = correlator;
		this.service = service;
		this.feedback = feedback;
		this.diagnostic = diagnostic;
		this.nanoTime = nanoTime;
		this.accountUnlinked = accountUnlinked;
		this.activityTiming = activityTiming;
		this.activityContextStarted = activityContextStarted;
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
		onActivityCompletion(source, reporter, null, clientSessionId, gameCycle, consent, diagnostics, now);
	}

	void onActivityCompletion(String source, String reporter, GroupSnapshot stableRoster, String clientSessionId,
		long gameCycle, boolean consent, boolean diagnostics, Instant now)
	{
		onActivityCompletion(source, reporter, stableRoster, clientSessionId, gameCycle,
			consent, diagnostics, now, null);
	}

	void onActivityCompletion(String source, String reporter, GroupSnapshot stableRoster, String clientSessionId,
		long gameCycle, boolean consent, boolean diagnostics, Instant now, String lootRecordId)
	{
		if (!consent) { clearPartial(); return; }
		long completionNanos = nanoTime.getAsLong();
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource(source);
		BossCompletionContext context = activity == null ? null : activity.challengeBossKey != null
			? BossCompletionContext.fromNpcLoot(source, reporter, clientSessionId, gameCycle,
				completionNanos, now, lootRecordId)
			: BossCompletionContext.fromTrackedActivity(source, reporter, stableRoster, clientSessionId,
				gameCycle, completionNanos, now, lootRecordId);
		if (context != null) activityContextStarted.accept(context);
		ChallengeObservation observation = genericCorrelator.onCompletion(context, completionNanos, now);
		if (observation != null) submit(observation, diagnostics);
	}

	void onGauntletCompletion(String category, String text, boolean activeGauntlet, String reporter,
		String clientSessionId, long gameCycle, boolean consent, boolean diagnostics, Instant now)
	{
		if (!consent) { clearPartial(); return; }
		if (!GauntletCompletionSignal.isCompletion(category, text, activeGauntlet)) return;
		long completionNanos = nanoTime.getAsLong();
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", reporter, clientSessionId,
			gameCycle, completionNanos, now);
		if (context != null) activityContextStarted.accept(context);
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
		activityTiming.accept(observation, ActivityTimingStatus.SENDING);
		boolean pluginPrimary = observation.pluginTrackingCandidate || !observation.challengeProjection;
		String diagnosticSource = pluginPrimary ? "Plugin Tracking observation" : "automatic Challenge observation";
		java.util.function.Consumer<ChallengeObservationResponse> primaryCallback = result ->
		{
			if (result.httpStatus >= 400)
				diagnostic.accept(diagnosticSource + " response http_status=" + result.httpStatus
					+ " state=" + result.state.name().toLowerCase(java.util.Locale.ROOT)
					+ " reason=" + (result.reason == null ? "none" : result.reason));
			switch (result.state)
			{
				case ACCEPTED:
					activityTiming.accept(observation, ActivityTimingStatus.ACCEPTED);
					feedback.accept("Nocturne recorded your " + observation.displayName + " time: "
						+ ChallengeObservation.formatTime(observation.displayDurationMillis) + "."); break;
				case DUPLICATE:
					activityTiming.accept(observation, ActivityTimingStatus.DUPLICATE);
					feedback.accept("Nocturne already recorded this completion."); break;
				case SERVER_FAILURE:
					activityTiming.accept(observation, ActivityTimingStatus.FAILED);
					feedback.accept("Nocturne could not confirm this completion. Please try again later."); break;
				case RATE_LIMITED:
					activityTiming.accept(observation, ActivityTimingStatus.FAILED);
					feedback.accept("Nocturne is receiving reports too quickly. This completion was not confirmed."); break;
				case INVALID:
					if ("reporter_not_eligible".equals(result.reason))
					{
						activityTiming.accept(observation, ActivityTimingStatus.LOCAL_ONLY);
						accountUnlinked.accept(observation);
						feedback.accept("Local only — account not linked");
					}
					else
					{
						activityTiming.accept(observation, ActivityTimingStatus.FAILED);
						if (diagnostics)
							diagnostic.accept(diagnosticSource + " rejected category=invalid");
					}
					break;
				case CONFLICT: case PROTOCOL_ERROR:
					activityTiming.accept(observation, ActivityTimingStatus.FAILED);
					if (diagnostics && result.httpStatus < 400)
						diagnostic.accept(diagnosticSource + " rejected category="
							+ result.state.name().toLowerCase(java.util.Locale.ROOT));
					break;
				case IGNORED:
					activityTiming.accept(observation, ActivityTimingStatus.LOCAL_ONLY);
					break;
				default: break;
			}
		};
		String id = pluginPrimary
			? service.submitPlugin(observation, primaryCallback)
			: service.submitChallengeProjection(observation, primaryCallback);
		if (id == null)
		{
			activityTiming.accept(observation, ActivityTimingStatus.FAILED);
			feedback.accept("Nocturne could not queue this completion. Please try again later.");
			if (diagnostics) diagnostic.accept(diagnosticSource + " skipped category=queue_full");
		}
		if (pluginPrimary && observation.challengeCandidate)
		{
			String challengeId = service.submitChallengeProjection(observation, result ->
			{
				if (diagnostics && result.state != ChallengeObservationResponse.State.ACCEPTED
					&& result.state != ChallengeObservationResponse.State.DUPLICATE
					&& result.state != ChallengeObservationResponse.State.IGNORED)
					diagnostic.accept("automatic Challenge projection state="
						+ result.state.name().toLowerCase(java.util.Locale.ROOT)
						+ " reason=" + (result.reason == null ? "none" : result.reason));
			});
			if (challengeId == null && diagnostics)
				diagnostic.accept("automatic Challenge projection skipped category=queue_full");
		}
	}
}
