package com.nocturne;

import java.util.Locale;

enum RaidType
{
	TOB("Theatre of Blood", "Party name slots"),
	TOA("Tombs of Amascut", "Party name slots"),
	COX("Chambers of Xeric", "Raiding-party sidebar");

	final String title;
	final String rosterSource;

	RaidType(String title, String rosterSource)
	{
		this.title = title;
		this.rosterSource = rosterSource;
	}

	static RaidType fromSource(String source)
	{
		if (source != null)
		{
			for (RaidType raid : values())
			{
				if (source.toLowerCase(Locale.ROOT).contains(raid.title.toLowerCase(Locale.ROOT)))
				{
					return raid;
				}
			}
		}
		return null;
	}
}
