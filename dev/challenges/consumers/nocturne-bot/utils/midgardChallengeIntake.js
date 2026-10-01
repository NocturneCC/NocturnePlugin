'use strict';

const { buildChallengeRecipients } = require('./challengeSubmissionIdentity');

// This boundary owns payload construction as well as transport: neither may
// reject into the detached caller. Do not log HTTP errors or request objects.
async function sendChallengeApprovalToMidgard(context) {
    let eventId;
    let status;
    const log = (category) => {
        try {
            console.warn('[midgard-challenge]', {
                event_id: eventId,
                http_status: status,
                category,
            });
        } catch (_) { /* Logging must never affect approval. */ }
    };

    try {
        const { interaction, embed, bossName, tierText, submittedValue,
            partyIds, imgLink, noteText, bossKey, configVersionId, metricType,
            resolveSubmitterRsn } = context;
        const messageId = interaction.message.id;
        if (!/^\d+$/.test(messageId)) {
            log('invalid_message_id');
            return;
        }
        eventId = `discord:challenge:${messageId}`;

        const url = process.env.MIDGARD_CHALLENGE_INTAKE_URL;
        const token = process.env.MIDGARD_CHALLENGE_INTAKE_TOKEN;
        if (process.env.MIDGARD_CHALLENGE_SHADOW_ENABLED !== 'true') {
            log('shadow_disabled');
            return;
        }
        if (!url || !token) {
            log('missing_configuration');
            return;
        }
        const destination = new URL(url);
        if (destination.protocol !== 'https:' || destination.username || destination.password) {
            log('invalid_configuration');
            return;
        }

        const field = (name) => embed.fields.find((item) => item.name === name)?.value;
        const submitterId = field('Submitter')?.match(/<@!?(\d+)>/)?.[1];
        let recipients;
        try {
            recipients = buildChallengeRecipients(submitterId, partyIds);
        } catch (_) {
            log('invalid_submitter_identity');
            return;
        }
        const approvalTimestamp = new Date().toISOString();
        let submittedRsn;
        try {
            if (submitterId) submittedRsn = await resolveSubmitterRsn(submitterId);
        } catch (_) { /* Only a sanitized warning below; no lookup error details. */ }
        if (typeof submittedRsn !== 'string' || !submittedRsn.trim()) {
            log('rsn_enrichment_failed');
            submittedRsn = undefined;
        }
        const payload = {
            provider: 'discord',
            event_id: eventId,
            discord_guild_id: interaction.guildId ?? interaction.guild?.id,
            discord_channel_id: interaction.channelId ?? interaction.channel?.id,
            discord_message_id: messageId,
            submitter_discord_id: recipients.submitterId,
            ...(submittedRsn ? { submitted_rsn: submittedRsn.trim() } : {}),
            approver_discord_id: interaction.user.id,
            boss: bossName,
            ...(bossKey ? { boss_key: bossKey } : {}),
            ...(Number.isInteger(configVersionId) ? { config_version_id: configVersionId } : {}),
            earned_tier: tierText,
            ...((metricType || (field('Wave') !== undefined ? 'numeric'
                : field('Completion') !== undefined ? 'completion' : 'time')) !== 'completion'
                ? { raw_metric: submittedValue } : {}),
            party_members: recipients.additionalPartyIds.map((discord_id) => ({ discord_id })),
            evidence_url: imgLink,
            raw_notes: noteText,
            approval_timestamp: approvalTimestamp,
        };

        // Native fetch gives a total deadline, including connection setup.
        // Reject redirects so the bearer token cannot follow another location.
        const response = await fetch(destination, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify(payload),
            redirect: 'error',
            signal: AbortSignal.timeout(3000),
        });
        status = response.status;
        if (!response.ok) {
            let category = 'http_error';
            try {
                // Inspect only the error code; never log receiver body contents.
                const result = await response.json();
                if (result?.error === 'submitter_identity_unresolved'
                    || result?.code === 'submitter_identity_unresolved') {
                    category = 'submitter_identity_unresolved';
                }
            } catch (_) { /* Non-JSON or unreadable errors remain HTTP errors. */ }
            log(category);
        }
        try { await response.body?.cancel(); } catch (_) { /* Body may already be consumed. */ }
    } catch (error) {
        log(error?.name === 'TimeoutError' || error?.name === 'AbortError'
            ? 'timeout' : 'request_failed');
    }
}

module.exports = { sendChallengeApprovalToMidgard };
