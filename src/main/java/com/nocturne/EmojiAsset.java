package com.nocturne;

import java.awt.image.BufferedImage;

/** One validated public emoji asset. */
final class EmojiAsset
{
	final String name;
	final String digest;
	final BufferedImage image;

	EmojiAsset(String name, String digest, BufferedImage image)
	{
		this.name = name;
		this.digest = digest;
		this.image = image;
	}
}
