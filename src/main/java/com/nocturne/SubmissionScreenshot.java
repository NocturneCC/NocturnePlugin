package com.nocturne;

import java.util.Base64;

/** A bounded in-memory JPEG captured from the RuneLite canvas. */
final class SubmissionScreenshot
{
	final String mimeType;
	final int width;
	final int height;
	final byte[] bytes;
	final String sha256;

	SubmissionScreenshot(String mimeType, int width, int height, byte[] bytes, String sha256)
	{
		this.mimeType = mimeType;
		this.width = width;
		this.height = height;
		this.bytes = bytes.clone();
		this.sha256 = sha256;
	}

	String base64()
	{
		return Base64.getEncoder().encodeToString(bytes);
	}
}
