package com.nocturne;

import java.time.Instant;

/** Validated, public announcement content. Contains no player or client identity. */
final class Announcement
{
	static final String PREFIX = "[Nocturne Announcement]";

	final String id;
	final int revision;
	final String title;
	final String message;
	final String severity;
	final Instant startsAt;
	final Instant expiresAt;
	final String linkLabel;
	final String linkUrl;

	Announcement(String id, int revision, String title, String message, String severity,
		Instant startsAt, Instant expiresAt, String linkLabel, String linkUrl)
	{
		this.id = id;
		this.revision = revision;
		this.title = title;
		this.message = message;
		this.severity = severity;
		this.startsAt = startsAt;
		this.expiresAt = expiresAt;
		this.linkLabel = linkLabel;
		this.linkUrl = linkUrl;
	}

	String chatText()
	{
		return PREFIX + " " + (title == null ? "" : title + ": ") + message;
	}
}
