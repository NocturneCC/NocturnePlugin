package com.nocturne;

import java.util.Collection;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

/** Immutable local evidence and client-side eligibility proposal; never a server award decision. */
final class GroupSnapshot
{
	enum Status { MATCHED, INCOMPLETE, OBSERVED, UNAVAILABLE }

	final String source;
	final List<String> names;
	final int expectedSize;
	final Status status;
	final String detail;
	final Boolean submissionEligible;
	final String eligibilityNote;

	GroupSnapshot(String source, Collection<String> names, int expectedSize, Status status, String detail)
	{
		this(source, names, expectedSize, status, detail, null, null);
	}

	GroupSnapshot(String source, Collection<String> names, int expectedSize, Status status, String detail,
		Boolean submissionEligible, String eligibilityNote)
	{
		this.source = source;
		this.names = uniqueNames(names);
		this.expectedSize = expectedSize;
		this.status = status;
		this.detail = detail;
		this.submissionEligible = submissionEligible;
		this.eligibilityNote = eligibilityNote;
	}

	boolean allowsSubmission()
	{
		return submissionEligible == null || submissionEligible;
	}

	static List<String> uniqueNames(Collection<String> names)
	{
		Map<String, String> unique = new TreeMap<>();
		for (String name : names)
		{
			if (name != null && !name.trim().isEmpty() && !"-".equals(name.trim()))
			{
				String trimmed = name.trim();
				unique.putIfAbsent(trimmed.toLowerCase(Locale.ROOT), trimmed);
			}
		}
		return List.copyOf(unique.values());
	}

	static GroupSnapshot unavailable(String detail)
	{
		return new GroupSnapshot("No roster", List.of(), 0, Status.UNAVAILABLE, detail);
	}

	String displayText()
	{
		String heading;
		switch (status)
		{
			case MATCHED: heading = "Roster matches observed size"; break;
			case INCOMPLETE: heading = "Capture incomplete"; break;
			case OBSERVED: heading = "Nearby observations only"; break;
			default: heading = "Group unavailable";
		}
		String size = expectedSize > 0 ? names.size() + " / " + expectedSize : names.size() + " / unknown";
		return heading + "\n" + source + "\nNames / observed size: " + size
			+ "\n" + detail + (names.isEmpty() ? "" : "\n" + String.join("\n", names));
	}
}
