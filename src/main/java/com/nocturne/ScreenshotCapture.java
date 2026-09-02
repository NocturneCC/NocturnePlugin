package com.nocturne;

import java.awt.Color;
import java.awt.Font;
import java.awt.FontMetrics;
import java.awt.Graphics2D;
import java.awt.Image;
import java.awt.Rectangle;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.List;
import java.util.Iterator;
import javax.imageio.IIOImage;
import javax.imageio.ImageIO;
import javax.imageio.ImageWriteParam;
import javax.imageio.ImageWriter;
import javax.imageio.stream.ImageOutputStream;

/** Converts one rendered RuneLite frame into a small, non-persistent JPEG. */
final class ScreenshotCapture
{
	static final int ELIGIBLE_UNIT_PRICE = 500_000;
	static final int MAX_BYTES = 240 * 1024;
	private static final int MAX_WIDTH = 960;
	private static final int MAX_HEIGHT = 720;
	private static final int FOOTER_HEIGHT = 42;
	private static final int FOOTER_PADDING = 8;
	private static final float[] QUALITIES = {0.78f, 0.64f, 0.50f, 0.36f};

	private ScreenshotCapture()
	{
	}

	static boolean isLikelyEligible(List<LootItem> items)
	{
		return items.stream().anyMatch(item -> item.unitPriceGp >= ELIGIBLE_UNIT_PRICE);
	}

	static SubmissionScreenshot encode(Image frame, Rectangle viewport, boolean includeChat,
		LootRecord record, String pluginVersion)
	{
		if (frame == null)
		{
			return null;
		}
		BufferedImage canvas = toRgb(frame);
		BufferedImage selected = includeChat ? canvas : crop(canvas, viewport);
		BufferedImage scaled = scale(withFooter(selected, footerLines(record, pluginVersion)));
		while (scaled.getWidth() >= 160 && scaled.getHeight() >= 120)
		{
			for (float quality : QUALITIES)
			{
				byte[] bytes = jpeg(scaled, quality);
				if (bytes != null && bytes.length <= MAX_BYTES)
				{
					return new SubmissionScreenshot("image/jpeg", scaled.getWidth(), scaled.getHeight(),
						bytes, sha256(bytes));
				}
			}
			scaled = resize(scaled, Math.max(1, (int) (scaled.getWidth() * .85)),
				Math.max(1, (int) (scaled.getHeight() * .85)));
		}
		return null;
	}

	static List<String> footerLines(LootRecord record, String pluginVersion)
	{
		String identity = "RSN: " + display(record.rsn, 24) + "  |  Source: " + display(record.source, 64);
		String safeId = display(record.id, 64);
		String event = safeId.substring(0, Math.min(8, safeId.length()));
		return List.of(identity, "UTC: " + record.occurredAt + "  |  Event: " + event
			+ "  |  Nocturne: " + display(pluginVersion, 16));
	}

	private static String display(String value, int limit)
	{
		String safe = value == null ? "Unavailable" : value.replaceAll("[\\p{Cntrl}]+", " ")
			.replaceAll("\\s+", " ").trim();
		if (safe.length() <= limit)
		{
			return safe;
		}
		return safe.substring(0, Math.max(1, limit - 1)) + "\u2026";
	}

	private static BufferedImage withFooter(BufferedImage image, List<String> lines)
	{
		BufferedImage result = new BufferedImage(image.getWidth(), image.getHeight() + FOOTER_HEIGHT,
			BufferedImage.TYPE_INT_RGB);
		Graphics2D graphics = result.createGraphics();
		try
		{
			graphics.drawImage(image, 0, 0, null);
			graphics.setColor(new Color(24, 27, 31));
			graphics.fillRect(0, image.getHeight(), result.getWidth(), FOOTER_HEIGHT);
			graphics.setColor(new Color(225, 228, 232));
			graphics.setFont(new Font(Font.SANS_SERIF, Font.PLAIN, 11));
			FontMetrics metrics = graphics.getFontMetrics();
			for (int index = 0; index < lines.size(); index++)
			{
				graphics.drawString(fit(lines.get(index), metrics, result.getWidth() - 2 * FOOTER_PADDING),
					FOOTER_PADDING, image.getHeight() + 15 + index * 16);
			}
		}
		finally
		{
			graphics.dispose();
		}
		return result;
	}

	private static String fit(String value, FontMetrics metrics, int width)
	{
		if (metrics.stringWidth(value) <= width)
		{
			return value;
		}
		String suffix = "\u2026";
		int end = value.length();
		while (end > 1 && metrics.stringWidth(value.substring(0, end) + suffix) > width)
		{
			end--;
		}
		return value.substring(0, end) + suffix;
	}

	private static BufferedImage toRgb(Image image)
	{
		int width = Math.max(1, image.getWidth(null));
		int height = Math.max(1, image.getHeight(null));
		BufferedImage result = new BufferedImage(width, height, BufferedImage.TYPE_INT_RGB);
		Graphics2D graphics = result.createGraphics();
		try
		{
			graphics.drawImage(image, 0, 0, null);
		}
		finally
		{
			graphics.dispose();
		}
		return result;
	}

	private static BufferedImage crop(BufferedImage image, Rectangle requested)
	{
		if (requested == null)
		{
			return image;
		}
		Rectangle bounds = new Rectangle(0, 0, image.getWidth(), image.getHeight());
		Rectangle safe = bounds.intersection(requested);
		if (safe.width < 32 || safe.height < 32)
		{
			return image;
		}
		BufferedImage copy = new BufferedImage(safe.width, safe.height, BufferedImage.TYPE_INT_RGB);
		Graphics2D graphics = copy.createGraphics();
		try
		{
			graphics.drawImage(image, 0, 0, safe.width, safe.height,
				safe.x, safe.y, safe.x + safe.width, safe.y + safe.height, null);
		}
		finally
		{
			graphics.dispose();
		}
		return copy;
	}

	private static BufferedImage scale(BufferedImage image)
	{
		double ratio = Math.min(1d, Math.min((double) MAX_WIDTH / image.getWidth(),
			(double) MAX_HEIGHT / image.getHeight()));
		if (ratio >= 1d)
		{
			return image;
		}
		int width = Math.max(1, (int) Math.round(image.getWidth() * ratio));
		int height = Math.max(1, (int) Math.round(image.getHeight() * ratio));
		return resize(image, width, height);
	}

	private static BufferedImage resize(BufferedImage image, int width, int height)
	{
		BufferedImage result = new BufferedImage(width, height, BufferedImage.TYPE_INT_RGB);
		Graphics2D graphics = result.createGraphics();
		try
		{
			graphics.setRenderingHint(RenderingHints.KEY_INTERPOLATION,
				RenderingHints.VALUE_INTERPOLATION_BILINEAR);
			graphics.drawImage(image, 0, 0, width, height, null);
		}
		finally
		{
			graphics.dispose();
		}
		return result;
	}

	private static byte[] jpeg(BufferedImage image, float quality)
	{
		Iterator<ImageWriter> writers = ImageIO.getImageWritersByFormatName("jpeg");
		if (!writers.hasNext())
		{
			return null;
		}
		ImageWriter writer = writers.next();
		try (ByteArrayOutputStream output = new ByteArrayOutputStream();
			 ImageOutputStream imageOutput = ImageIO.createImageOutputStream(output))
		{
			writer.setOutput(imageOutput);
			ImageWriteParam params = writer.getDefaultWriteParam();
			params.setCompressionMode(ImageWriteParam.MODE_EXPLICIT);
			params.setCompressionQuality(quality);
			writer.write(null, new IIOImage(image, null, null), params);
			imageOutput.flush();
			return output.toByteArray();
		}
		catch (IOException ignored)
		{
			return null;
		}
		finally
		{
			writer.dispose();
		}
	}

	private static String sha256(byte[] bytes)
	{
		try
		{
			byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
			StringBuilder hex = new StringBuilder(digest.length * 2);
			for (byte value : digest)
			{
				hex.append(String.format("%02x", value & 0xff));
			}
			return hex.toString();
		}
		catch (NoSuchAlgorithmException impossible)
		{
			throw new IllegalStateException(impossible);
		}
	}
}
