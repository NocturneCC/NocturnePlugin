package com.nocturne;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashSet;
import java.util.Set;
import java.util.function.LongSupplier;

/** Session-only queue for sequential announcement overlays. */
final class AnnouncementToastQueue implements AutoCloseable
{
	static final long DISPLAY_NANOS = 60_000_000_000L;
	private final LongSupplier nanoTime;
	private final Deque<Announcement> pending = new ArrayDeque<>();
	private final Set<Revision> seen = new HashSet<>();
	private Announcement active;
	private long activeSince;
	private boolean activeStarted;
	private boolean closed;

	AnnouncementToastQueue(LongSupplier nanoTime)
	{
		this.nanoTime = nanoTime;
	}

	synchronized boolean enqueue(Announcement announcement)
	{
		if (closed || announcement == null) return false;
		Revision revision = new Revision(announcement.id, announcement.revision);
		if (!seen.add(revision)) return false;
		if (active == null)
		{
			active = announcement;
			activeStarted = false;
		}
		else
		{
			pending.addLast(announcement);
		}
		return true;
	}

	synchronized Display current()
	{
		long now = nanoTime.getAsLong();
		if (active != null && activeStarted && now - activeSince > DISPLAY_NANOS)
		{
			active = pending.pollFirst();
			activeStarted = false;
		}
		if (active == null) return null;
		if (!activeStarted)
		{
			activeSince = now;
			activeStarted = true;
		}
		long elapsedNanos = Math.max(0, now - activeSince);
		long remainingNanos = Math.max(0, DISPLAY_NANOS - elapsedNanos);
		int remaining = (int) ((remainingNanos + 999_999_999L) / 1_000_000_000L);
		return new Display(active, remaining);
	}

	synchronized int pendingCount()
	{
		return pending.size();
	}

	synchronized void clear()
	{
		closed = true;
		active = null;
		pending.clear();
		seen.clear();
	}

	@Override public void close()
	{
		clear();
	}

	static final class Display
	{
		final Announcement announcement;
		final int secondsRemaining;

		private Display(Announcement announcement, int secondsRemaining)
		{
			this.announcement = announcement;
			this.secondsRemaining = secondsRemaining;
		}

		String text()
		{
			String title = announcement.title == null ? "" : announcement.title + " — ";
			int minutes = secondsRemaining / 60;
			int seconds = secondsRemaining % 60;
			return "Clan announcement (" + minutes + ":" + (seconds < 10 ? "0" : "")
				+ seconds + "): " + title + announcement.message.replace('\n', ' ').replace('\r', ' ');
		}
	}

	private static final class Revision
	{
		private final String id;
		private final int revision;

		private Revision(String id, int revision)
		{
			this.id = id;
			this.revision = revision;
		}

		@Override public boolean equals(Object other)
		{
			if (this == other) return true;
			if (!(other instanceof Revision)) return false;
			Revision that = (Revision) other;
			return revision == that.revision && id.equals(that.id);
		}

		@Override public int hashCode()
		{
			return 31 * id.hashCode() + revision;
		}
	}
}
