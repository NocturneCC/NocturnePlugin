'use strict';

const DISCORD_ID = /^\d{15,22}$/;

function normalizeDiscordId(value, fieldName = 'Discord ID') {
    const id = String(value ?? '').trim();
    if (!DISCORD_ID.test(id)) {
        throw new Error(`${fieldName} is missing or invalid`);
    }
    return id;
}

function mentionIds(value) {
    const ids = [];
    const text = String(value ?? '');
    for (const match of text.matchAll(/<@!?(\d+)>/g)) {
        if (!ids.includes(match[1])) ids.push(match[1]);
    }
    return ids;
}

function buildChallengeRecipients(submitterDiscordId, partyDiscordIds = []) {
    const submitterId = normalizeDiscordId(
        submitterDiscordId,
        'Challenge submitter Discord ID'
    );
    const additionalPartyIds = [];
    for (const value of partyDiscordIds) {
        const id = normalizeDiscordId(value, 'Challenge party Discord ID');
        if (id !== submitterId && !additionalPartyIds.includes(id)) {
            additionalPartyIds.push(id);
        }
    }
    return {
        submitterId,
        additionalPartyIds,
        recipientIds: [submitterId, ...additionalPartyIds],
    };
}

module.exports = {
    buildChallengeRecipients,
    mentionIds,
    normalizeDiscordId,
};
