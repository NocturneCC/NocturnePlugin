const {
  EmbedBuilder,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
  StringSelectMenuBuilder,
} = require('discord.js');
const { MessageFlags } = require('discord.js');
const submit = require('../commands/utility/submit');
const config = require('../config');
const Fuse = require('fuse.js');
const pets = require('../pets.json');
const fs = require('fs');
const { google } = require('googleapis');
const { manager: challengeConfig, selectMenuEmoji } = require('./challengeConfig');
const { buildChallengeRecipients } = require('./challengeSubmissionIdentity');
const { calculateChallengeTier, submissionPartyPolicy } = require('./challengeMetric');

// load the credentials json
const credentials = JSON.parse(fs.readFileSync('nocturnesheets-313c26c4ef3b.json'));

// Configure the Google Sheets API client
const auth = new google.auth.GoogleAuth({
  credentials: credentials,
  scopes: ['https://www.googleapis.com/auth/spreadsheets'],
});

const sheets = google.sheets({ version: 'v4', auth });

const fuse = new Fuse(pets, {
  keys: ['name', 'aliases'],
  threshold: config.fuseThreshold,
});

// -----------------------------
// NEW TIME SUBMISSION DATA
// -----------------------------
const timeSessions = new Map();

function getChallengeByName(bossKey) {
  return challengeConfig.getBossByKey(bossKey);
}

function getTierByName(challenge, tierName) {
  if (!challenge) return null;
  return challenge.tiers.find((t) => t.key === tierName) || null;
}

async function waitForOneMessage(
  channel,
  filter,
  timeoutMessageText,
  interaction,
  stillActive = null
) {
  const collected = await channel.awaitMessages({
    filter,
    max: 1,
    time: 60000,
  });

  if (!collected || collected.size === 0) {
    // If this submission was replaced by another submission,
    // silently abandon the old flow instead of sending a stale timeout.
    if (stillActive && !stillActive()) {
      return null;
    }

    await interaction.followUp({
      content: timeoutMessageText,
      flags: MessageFlags.Ephemeral,
    });
    return null;
  }

  return collected.first();
}

/**
 * Handles the process of asking for drop details from a user
 * @param {Object} interaction - The Discord interaction object
 * @returns {Promise<void>}
 */

// ============================================================
// UNIVERSAL SUBMISSION SESSION MANAGER
//
// One Discord user may only have one active submission flow.
// Different Discord users can still submit simultaneously.
// ============================================================

const activeSubmissionSessions = new Map();

function beginSubmissionSession(userId, type) {
  const previous = activeSubmissionSessions.get(userId);

  if (previous) {
    for (const collector of previous.collectors) {
      try {
        if (!collector.ended) {
          collector.stop('replaced');
        }
      } catch (error) {
        console.error('Unable to stop old submission collector:', error);
      }
    }

    // Invalidate an old challenge/time session too.
    if (typeof timeSessions !== 'undefined') {
      timeSessions.delete(userId);
    }
  }

  const session = {
    sessionId: `${Date.now()}_${Math.random().toString(36).slice(2)}`,
    type,
    collectors: new Set(),
  };

  activeSubmissionSessions.set(userId, session);

  console.log(
    `Submission session started: user=${userId} type=${type}` +
    (previous ? ` replacing=${previous.type}` : '')
  );

  return {
    session,
    replaced: Boolean(previous),
  };
}

function isCurrentSubmissionSession(userId, sessionId) {
  const session = activeSubmissionSessions.get(userId);

  return Boolean(
    session &&
    session.sessionId === sessionId
  );
}

function registerSubmissionCollector(userId, sessionId, collector) {
  const session = activeSubmissionSessions.get(userId);

  if (!session || session.sessionId !== sessionId) {
    try {
      collector.stop('replaced');
    } catch (_) {}

    return collector;
  }

  session.collectors.add(collector);

  collector.once('end', () => {
    const current = activeSubmissionSessions.get(userId);

    if (current && current.sessionId === sessionId) {
      current.collectors.delete(collector);
    }
  });

  return collector;
}

function finishSubmissionSession(userId, sessionId) {
  const session = activeSubmissionSessions.get(userId);

  if (!session || session.sessionId !== sessionId) {
    return;
  }

  for (const collector of session.collectors) {
    try {
      if (!collector.ended) {
        collector.stop('completed');
      }
    } catch (_) {}
  }

  activeSubmissionSessions.delete(userId);

  console.log(
    `Submission session finished: user=${userId} type=${session.type}`
  );
}

async function askDropDetails(interaction) {
  const userId = interaction.user.id;
  const { session, replaced } = beginSubmissionSession(userId, 'drop');
  const sessionId = session.sessionId;

  const filter = (m) =>
    m.author.id === userId &&
    isCurrentSubmissionSession(userId, sessionId);

  const client = interaction.client;

  await interaction.reply({
    content: replaced
      ? 'Your previous submission was cancelled and replaced with this Drop submission.\n\nWhat was the drop?'
      : 'What was the drop?',
    flags: MessageFlags.Ephemeral
  });
  console.log('ask for drop');

  const dropTypeCollector = registerSubmissionCollector(
    userId,
    sessionId,
    interaction.channel.createMessageCollector({ filter, max: 1, time: 60000 })
  );

  dropTypeCollector.on('collect', async (message) => {
    const dropType = message.content;
    console.log(dropType);
    await message.delete();
    await interaction.followUp({ content: 'What was the value of the drop?', flags: MessageFlags.Ephemeral });

    console.log('ask for value of drop');

    const dropValueCollector = registerSubmissionCollector(
      userId,
      sessionId,
      interaction.channel.createMessageCollector({ filter, max: 1, time: 60000 })
    );

    dropValueCollector.on('collect', async (message) => {
      const dropValue = message.content;
      console.log(dropValue);
      await message.delete();
      await interaction.followUp({
        content: 'Who was involved in the drop? Please tag all involved users, including yourself.',
        flags: MessageFlags.Ephemeral,
      });
      console.log('ask for who was involved with drop');

      const usersCollector = registerSubmissionCollector(
        userId,
        sessionId,
        interaction.channel.createMessageCollector({ filter, max: 1, time: 180000 })
      );

      usersCollector.on('collect', async (message) => {
        const involvedUsers = message.mentions.users;
        console.log('Mentions count' + involvedUsers.size);

        if (involvedUsers.size === 0) {
          await interaction.followUp({ content: 'Please start over and mention at least one user.', flags: MessageFlags.Ephemeral });
          return;
        }

        await message.delete();
        await interaction.followUp({
          content: 'Please submit your photo by uploading it or by providing a URL in your next message.',
          flags: MessageFlags.Ephemeral,
        });
        console.log('ask for pic of drop');

        const imageCollector = registerSubmissionCollector(
          userId,
          sessionId,
          interaction.channel.createMessageCollector({ filter, max: 1, time: 60000 })
        );

        imageCollector.on('collect', async (message) => {
          let imageUrl;

          if (message.attachments.size > 0) {
            const attachment = message.attachments.first();
            imageUrl = attachment.url;
          } else if (message.content.match(/^https?:\/\/.*\.(jpeg|jpg|png|gif)(\?.*)?$/i)) {
            imageUrl = message.content;
          }

          if (imageUrl) {
            const embed = new EmbedBuilder()
              .setColor(0x00FF00)
              .setTitle('Drop Submission')
              .addFields(
                { name: 'Submitter', value: `<@${interaction.user.id}>`, inline: false },
                { name: 'Drop', value: dropType, inline: false },
                { name: 'Value', value: dropValue, inline: false },
                { name: 'With', value: involvedUsers.map((user) => `<@${user.id}>`).join(', '), inline: false }
              )
              .setImage(imageUrl)
              .setTimestamp();

            const approveButton = new ButtonBuilder()
              .setCustomId('approve_drop')
              .setLabel('Approve')
              .setStyle(ButtonStyle.Success);

            const denyButton = new ButtonBuilder()
              .setCustomId('deny_drop')
              .setLabel('Deny')
              .setStyle(ButtonStyle.Danger);

            const row = new ActionRowBuilder().addComponents(approveButton, denyButton);

            const approvalChannel = client.channels.cache.get(config.targetSubmissionsChannelId);
            if (approvalChannel) {
              await approvalChannel.send({ embeds: [embed], components: [row] });
              await interaction.followUp({ content: 'Your submission has been sent for approval.', flags: MessageFlags.Ephemeral });
              finishSubmissionSession(userId, sessionId);
              await submit.addButton(interaction);
            } else {
              await interaction.followUp({ content: 'Error: Approval channel not found.', flags: MessageFlags.Ephemeral });
            }
          } else {
            await message.reply({ content: 'Please submit a valid image file or URL.', flags: MessageFlags.Ephemeral });
          }
        });

        imageCollector.on('end', (collected) => {
          if (
            collected.size === 0 &&
            isCurrentSubmissionSession(userId, sessionId)
          ) {
            finishSubmissionSession(userId, sessionId);
            interaction.followUp({ content: 'You did not submit a photo in time.', flags: MessageFlags.Ephemeral });
          }
        });
      });

      usersCollector.on('end', (collected) => {
        if (
          collected.size === 0 &&
          isCurrentSubmissionSession(userId, sessionId)
        ) {
          finishSubmissionSession(userId, sessionId);
          interaction.followUp({ content: 'You did not provide information in time.', flags: MessageFlags.Ephemeral });
        }
      });
    });

    dropValueCollector.on('end', (collected) => {
      if (collected.size === 0) {
        interaction.followUp({ content: 'You did not provide information in time.', flags: MessageFlags.Ephemeral });
      }
    });
  });

  dropTypeCollector.on('end', (collected) => {
    if (
      collected.size === 0 &&
      isCurrentSubmissionSession(userId, sessionId)
    ) {
      finishSubmissionSession(userId, sessionId);
      interaction.followUp({ content: 'You did not provide information in time.', flags: MessageFlags.Ephemeral });
    }
  });
}

/**
 * Handles the process of submitting a pet drop
 * @param {Object} interaction - The Discord interaction object
 * @returns {Promise<void>}
 */

function normalizePetText(value) {
  return String(value || '')
    .trim()
    .toLowerCase();
}

function findExactPetMatch(query) {
  const normalized = normalizePetText(query);

  return pets.find((pet) => {
    if (normalizePetText(pet.name) === normalized) {
      return true;
    }

    const aliases = Array.isArray(pet.aliases)
      ? pet.aliases
      : pet.aliases
        ? [pet.aliases]
        : [];

    return aliases.some(
      (alias) => normalizePetText(alias) === normalized
    );
  }) || null;
}

function getPetMatches(query, limit = 10) {
  const normalized = normalizePetText(query);

  if (!normalized) {
    return [];
  }

  // First prefer direct substring matches.
  const substringMatches = pets.filter((pet) => {
    const searchable = [
      pet.name,
      ...(Array.isArray(pet.aliases)
        ? pet.aliases
        : pet.aliases
          ? [pet.aliases]
          : [])
    ];

    return searchable.some((value) =>
      normalizePetText(value).includes(normalized)
    );
  });

  if (substringMatches.length > 0) {
    return substringMatches.slice(0, limit);
  }

  // Fall back to Fuse fuzzy matching.
  return fuse.search(query, { limit })
    .map((result) => result.item);
}

async function resolvePetMatch(
  interaction,
  userId,
  sessionId,
  initialQuery
) {
  const exact = findExactPetMatch(initialQuery);

  if (exact) {
    return exact;
  }

  const matches = getPetMatches(initialQuery, 10);

  if (matches.length === 0) {
    await interaction.followUp({
      content: `I couldn't find a Pets/Kits/Jars match for **${initialQuery}**. Please start over and try another name.`,
      flags: MessageFlags.Ephemeral
    });

    return null;
  }

  // If Fuse/substrings produced exactly one result,
  // just accept it automatically.
  if (matches.length === 1) {
    return matches[0];
  }

  const suggestions = matches
    .map((pet, index) => `${index + 1}. ${pet.name}`)
    .join('\n');

  await interaction.followUp({
    content:
      `I found multiple possible matches for **${initialQuery}**:\n\n` +
      `${suggestions}\n\n` +
      `Please type the exact name of the one you want.`,
    flags: MessageFlags.Ephemeral
  });

  const filter = (m) =>
    m.author.id === userId &&
    isCurrentSubmissionSession(userId, sessionId);

  const clarificationCollector = registerSubmissionCollector(
    userId,
    sessionId,
    interaction.channel.createMessageCollector({
      filter,
      max: 1,
      time: 60000
    })
  );

  return await new Promise((resolve) => {
    clarificationCollector.on('collect', async (message) => {
      const clarification = message.content.trim();

      await message.delete().catch(() => {});

      const exactClarified = findExactPetMatch(clarification);

      if (exactClarified) {
        resolve(exactClarified);
        return;
      }

      const clarifiedMatches = getPetMatches(clarification, 10);

      if (clarifiedMatches.length === 1) {
        resolve(clarifiedMatches[0]);
        return;
      }

      if (clarifiedMatches.length > 1) {
        await interaction.followUp({
          content:
            `That is still ambiguous. Please start over and use the full item name.\n\n` +
            clarifiedMatches
              .map((pet, index) => `${index + 1}. ${pet.name}`)
              .join('\n'),
          flags: MessageFlags.Ephemeral
        });
      } else {
        await interaction.followUp({
          content: `I still couldn't find **${clarification}**. Please start over and try the full item name.`,
          flags: MessageFlags.Ephemeral
        });
      }

      resolve(null);
    });

    clarificationCollector.on('end', (collected, reason) => {
      if (collected.size === 0 && reason !== 'replaced') {
        resolve(null);
      }
    });
  });
}

async function petDropDetails(interaction) {
  const userId = interaction.user.id;
  const { session, replaced } = beginSubmissionSession(userId, 'pet');
  const sessionId = session.sessionId;

  const filter = (m) =>
    m.author.id === userId &&
    isCurrentSubmissionSession(userId, sessionId);

  const client = interaction.client;

  await interaction.reply({
    content: replaced
      ? 'Your previous submission was cancelled and replaced with this Pets/Kits/Jars submission.\n\nWhat was the drop?'
      : 'What was the drop?',
    flags: MessageFlags.Ephemeral
  });

  console.log('ask for pet?');
  const channel = interaction.channel;

  const petCollector = registerSubmissionCollector(
    userId,
    sessionId,
    interaction.channel.createMessageCollector({ filter, max: 1, time: 60000 })
  );
  petCollector.on('collect', async (nameMsg) => {
    const petNameRaw = nameMsg.content.trim();

    await nameMsg.delete().catch(() => {});

    const match = await resolvePetMatch(
      interaction,
      userId,
      sessionId,
      petNameRaw
    );

    if (!match) {
      finishSubmissionSession(userId, sessionId);
      return;
    }

    if (!isCurrentSubmissionSession(userId, sessionId)) {
      return;
    }

    await interaction.followUp({
      content: `Selected **${match.name}**. Please submit a valid image file or URL.`,
      flags: MessageFlags.Ephemeral
    });

    const imageCollector = registerSubmissionCollector(
      userId,
      sessionId,
      interaction.channel.createMessageCollector({ filter, max: 1, time: 60000 })
    );

    imageCollector.on('collect', async (message) => {
      let imageUrl;
      if (message.attachments.size > 0) {
        const attachment = message.attachments.first();
        imageUrl = attachment.url;
      } else if (message.content.match(/^https?:\/\/.*\.(jpeg|jpg|png|gif)(\?.*)?$/i)) {
        imageUrl = message.content;
      }

      const reviewEmbed = new EmbedBuilder()
        .setColor(0x00FF00)
        .setTitle('Pet Submission')
        .addFields(
          { name: 'Submitter', value: `<@${userId}>`, inline: false },
          { name: 'Pet', value: match.name, inline: false },
          { name: 'Value', value: `${match.points}`, inline: false }
        )
        .setImage(imageUrl)
        .setTimestamp();

      const approveButton = new ButtonBuilder()
        .setCustomId('approve_pet')
        .setLabel('Approve')
        .setStyle(ButtonStyle.Success);

      const denyButton = new ButtonBuilder()
        .setCustomId('deny_pet')
        .setLabel('Deny')
        .setStyle(ButtonStyle.Danger);

      const row = new ActionRowBuilder().addComponents(approveButton, denyButton);

      const approvalChannel = await client.channels.fetch(config.targetSubmissionsChannelId);
      await approvalChannel.send({ embeds: [reviewEmbed], components: [row] });
      await interaction.followUp({ content: 'Your submission has been sent for approval.', flags: MessageFlags.Ephemeral });
      finishSubmissionSession(userId, sessionId);
      await submit.addButton(interaction);
    });
  });
}

function createTimeSession(userId) {
  const session = {
    sessionId: `${Date.now()}_${Math.random().toString(36).slice(2)}`,
    step: 'awaiting_challenge',
    submitted: false,
  };

  timeSessions.set(userId, session);
  return session;
}

function getTimeSession(userId) {
  return timeSessions.get(userId);
}

function isSameSession(userId, sessionId) {
  const current = timeSessions.get(userId);
  return current && current.sessionId === sessionId;
}

function clearTimeSession(userId) {
  timeSessions.delete(userId);
}


// -----------------------------
// REPLACED TIME SUBMISSION FLOW
// -----------------------------
async function timeBossList(interaction) {
  try {
    const userId = interaction.user.id;
    const { session: submissionSession, replaced } =
      beginSubmissionSession(userId, 'time');

    const timeSession = createTimeSession(userId);
    timeSession.submissionSessionId = submissionSession.sessionId;
    timeSessions.set(userId, timeSession);

    const options = challengeConfig.getSubmissionBosses().map((challenge) => {
      const emoji = selectMenuEmoji(challenge.emoji, interaction.client);
      return {
        label: challenge.name,
        value: challenge.bossKey,
        ...(emoji ? { emoji } : {}),
      };
    });

    const selectMenu = new StringSelectMenuBuilder()
      .setCustomId('time')
      .setPlaceholder('Select a challenge')
      .addOptions(options);

    const row = new ActionRowBuilder().addComponents(selectMenu);

    await interaction.reply({
      content: replaced
        ? 'Your previous submission was cancelled and replaced with this Challenge submission.\n\nSelect a challenge:'
        : 'Select a challenge:',
      components: [row],
      flags: MessageFlags.Ephemeral,
    });
  } catch (error) {
    console.error('Error in timeBossList:', error);
    if (!interaction.replied && !interaction.deferred) {
      await interaction.reply({
        content: 'There was an error loading the challenge list.',
        flags: MessageFlags.Ephemeral,
      });
    }
  }
}

async function timeDetails(interaction) {
  try {
    const session = getTimeSession(interaction.user.id);

    if (!session) {
  return interaction.reply({
    content: 'Your time submission session expired. Please click Submit Challenge again.',
    flags: MessageFlags.Ephemeral,
  });
}

const activeSessionId = session.sessionId;
const selectedValue = interaction.values[0];
    if (session.step !== 'awaiting_challenge') {
      return interaction.reply({
        content: 'Your Challenge submission is in an invalid state. Please start again.',
        flags: MessageFlags.Ephemeral,
      });
    }

      const challenge = getChallengeByName(selectedValue);
      if (!challenge) {
        return interaction.reply({
          content: 'That challenge could not be found. Please try again.',
          flags: MessageFlags.Ephemeral,
        });
      }
      session.challenge = challenge;
      session.step = 'collecting_details';
      timeSessions.set(interaction.user.id, session);

      const example = challenge.timeInputFormat === 'MM:SS.xx'
        ? '07:45.00' : '00:36:21.00';
      await interaction.update({
        content: challenge.inputType === 'wave'
          ? `You selected **${challenge.name}**. Enter the **wave count** reached.`
          : challenge.inputType === 'completion'
            ? `You selected **${challenge.name}**. No numeric metric is required.`
            : `You selected **${challenge.name}**. Enter the time as **${challenge.timeInputFormat}**.\nExample: **${example}**`,
        components: [],
      });

      const filter = (m) =>
        m.author.id === interaction.user.id &&
        isCurrentSubmissionSession(
          interaction.user.id,
          session.submissionSessionId
        );

      let submittedMetric = 'Completion';
      if (challenge.inputType !== 'completion') {
        const metricMsg = await waitForOneMessage(
          interaction.channel,
          filter,
          'You did not provide the time/wave in time.',
          interaction,
          () => isCurrentSubmissionSession(
            interaction.user.id,
            session.submissionSessionId
          )
        );
        if (!metricMsg) {
          clearTimeSession(interaction.user.id);
          return;
        }
        if (!isSameSession(interaction.user.id, activeSessionId)) {
          return;
        }
        submittedMetric = metricMsg.content.trim();
        await metricMsg.delete().catch(() => {});
      }

      let calculated;
      try {
        calculated = calculateChallengeTier(challenge, submittedMetric);
      } catch (error) {
        clearTimeSession(interaction.user.id);
        return interaction.followUp({
          content: `Invalid metric: ${error.message}. Your submission was cancelled; please start again.`,
          flags: MessageFlags.Ephemeral,
        });
      }
      const tier = calculated.tier;
      submittedMetric = calculated.metricDisplay;
      session.tier = tier;
      timeSessions.set(interaction.user.id, session);

      await interaction.followUp({
        content:
          `Calculated Tier: **${tier.name}**\n` +
          `Submitted Metric: **${submittedMetric}**\n` +
          `${tier.name} Requirement: **${calculated.requirement}**`,
        flags: MessageFlags.Ephemeral,
      });

      let recipients = buildChallengeRecipients(interaction.user.id, []);
      const partyPolicy = submissionPartyPolicy(challenge);
      if (partyPolicy.promptForParty) {
        await interaction.followUp({
          content: challenge.submissionMode === 'group'
            ? `**You are the primary recipient.** Tag the additional party members. At least **${challenge.minPartySize}** total participants are required.`
            : '**You are the primary recipient.** Tag optional additional party members, or type **solo**.',
          flags: MessageFlags.Ephemeral,
        });
        const mentionMsg = await waitForOneMessage(
          interaction.channel,
          filter,
          'You did not provide the party response in time.',
          interaction,
          () => isCurrentSubmissionSession(interaction.user.id, session.submissionSessionId)
        );
        if (!mentionMsg) {
          clearTimeSession(interaction.user.id);
          return;
        }
        if (!isSameSession(interaction.user.id, activeSessionId)) return;
        recipients = buildChallengeRecipients(
          interaction.user.id,
          mentionMsg.mentions.users.map((user) => user.id)
        );
        await mentionMsg.delete().catch(() => {});
        if (recipients.recipientIds.length < partyPolicy.minPartySize) {
          clearTimeSession(interaction.user.id);
          return interaction.followUp({
            content: `This challenge requires at least **${partyPolicy.minPartySize}** total participants. The submission was cancelled.`,
            flags: MessageFlags.Ephemeral,
          });
        }
      }

      // Image
      await interaction.followUp({
        content: 'Please submit your photo by uploading it or by providing a URL in your next message.',
        flags: MessageFlags.Ephemeral,
      });

      const imageMsg = await waitForOneMessage(
        interaction.channel,
        filter,
        'You did not submit a photo in time.',
        interaction,
        () => isCurrentSubmissionSession(
          interaction.user.id,
          session.submissionSessionId
        )
      );
      if (!imageMsg) {
        clearTimeSession(interaction.user.id);
        return;
      }
      if (!isSameSession(interaction.user.id, activeSessionId)) {
  return;
}
      let imageUrl = null;
      if (imageMsg.attachments.size > 0) {
        const attachment = imageMsg.attachments.first();
        imageUrl = attachment.url;
      } else if (imageMsg.content.match(/^https?:\/\/.*\.(jpeg|jpg|png|gif|webp)(\?.*)?$/i)) {
        imageUrl = imageMsg.content.trim();
      }

      if (!imageUrl) {
        clearTimeSession(interaction.user.id);
        return interaction.followUp({
          content: 'Please submit a valid image file or URL.',
          flags: MessageFlags.Ephemeral,
        });
      }

      const reviewEmbed = new EmbedBuilder()
        .setColor(0x00FF00)
        .setTitle('Time Submission')
        .addFields(
          { name: 'Submitter', value: `<@${interaction.user.id}>`, inline: false },
          { name: 'Boss', value: challenge.name, inline: false },
          { name: 'Calculated Tier', value: tier.name, inline: false },
          { name: 'Submitted Metric', value: submittedMetric, inline: false },
          { name: 'Tier Requirement', value: calculated.requirement, inline: false },
          { name: 'Metric Type', value: calculated.metricType, inline: false },
          { name: 'Points', value: `${tier.points}`, inline: false },
          ...(partyPolicy.mode === 'solo' ? [] : [{
            name: 'With',
            value: recipients.additionalPartyIds.length
              ? recipients.additionalPartyIds.map((id) => `<@${id}>`).join(', ')
              : 'None',
            inline: false,
          }])
        )
        .setImage(imageUrl)
        .setTimestamp();
      reviewEmbed.setFooter({ text: `challenge-config:${challenge.configVersionId}:${challenge.bossKey}` });

      const approveButton = new ButtonBuilder()
        .setCustomId('approve_time')
        .setLabel('Approve')
        .setStyle(ButtonStyle.Success);

      const denyButton = new ButtonBuilder()
        .setCustomId('deny_time')
        .setLabel('Deny')
        .setStyle(ButtonStyle.Danger);

      const row = new ActionRowBuilder().addComponents(approveButton, denyButton);

        const latestSession = getTimeSession(interaction.user.id);
if (!latestSession || latestSession.sessionId !== activeSessionId || latestSession.submitted) {
  return;
}

latestSession.submitted = true;
timeSessions.set(interaction.user.id, latestSession);

      const approvalChannel = await interaction.client.channels.fetch(config.targetSubmissionsChannelId);
      await approvalChannel.send({ embeds: [reviewEmbed], components: [row] });

      await interaction.followUp({
        content: 'Your submission has been sent for approval.',
        flags: MessageFlags.Ephemeral,
      });

      await submit.addButton(interaction);
      finishSubmissionSession(
        interaction.user.id,
        session.submissionSessionId
      );
      clearTimeSession(interaction.user.id);
      return;
  } catch (error) {
    console.error('Error in timeDetails:', error);
    clearTimeSession(interaction.user.id);

    if (!interaction.replied && !interaction.deferred) {
      await interaction.reply({
        content: 'There was an error processing the time submission.',
        flags: MessageFlags.Ephemeral,
      });
    } else {
      await interaction.followUp({
        content: 'There was an error processing the time submission.',
        flags: MessageFlags.Ephemeral,
      });
    }
  }
}

module.exports = {
  askDropDetails,
  petDropDetails,
  timeDetails,
  timeBossList,
};
