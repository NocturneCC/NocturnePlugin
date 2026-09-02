package com.nocturne;

import java.awt.Rectangle;
import java.awt.image.BufferedImage;
import java.util.List;
import java.util.Random;
import org.junit.Test;
import static org.junit.Assert.*;

public class ScreenshotCaptureTest
{
	@Test
	public void eligibilityUsesIndividualUnitPrice()
	{
		assertFalse(ScreenshotCapture.isLikelyEligible(LootItem.consolidate(List.of(
			new LootItem(1, 300_000, "Cheap stack", 1),
			new LootItem(1, 300_000, "Cheap stack", 1)))));
		assertTrue(ScreenshotCapture.isLikelyEligible(LootItem.consolidate(List.of(
			new LootItem(2, 1, "Eligible item", 500_000),
			new LootItem(2, 2, "Eligible item", 500_000)))));
	}

	@Test
	public void croppedJpegIsBoundedAndHasDigest()
	{
		BufferedImage frame = image(1200, 800);
		SubmissionScreenshot shot = ScreenshotCapture.encode(
			frame, new Rectangle(100, 80, 900, 600), false, record(), "0.3.2");
		assertNotNull(shot);
		assertEquals("image/jpeg", shot.mimeType);
		assertEquals(900, shot.width);
		assertEquals(642, shot.height);
		assertTrue(shot.bytes.length <= ScreenshotCapture.MAX_BYTES);
		assertEquals(64, shot.sha256.length());
		assertEquals((byte) 0xff, shot.bytes[0]);
		assertEquals((byte) 0xd8, shot.bytes[1]);
	}

	@Test
	public void fullCanvasIsScaledWhenChatIsIncluded()
	{
		SubmissionScreenshot shot = ScreenshotCapture.encode(
			image(1600, 1000), new Rectangle(10, 10, 100, 100), true, record(), "0.3.2");
		assertNotNull(shot);
		assertEquals(960, shot.width);
		assertTrue(shot.height <= 720);
	}

	@Test
	public void footerUsesPayloadMetadataAndExcludesPrivateRecordFields()
	{
		LootRecord record = new LootRecord("12345678-1234-1234-1234-123456789abc",
			"2026-09-02T16:38:22Z", "De\nLena", "A very long Chambers source name that must remain compact and safe despite its length",
			List.of(new LootItem(1, 1, "Access token secret", 9_999_999)),
			new GroupSnapshot("raid", List.of("Private Party Member"), 2,
				GroupSnapshot.Status.MATCHED, "/private/path"), SubmissionStatus.LOCAL);
		String footer = String.join("\n", ScreenshotCapture.footerLines(record, "0.3.2"));
		assertTrue(footer.contains("De Lena"));
		assertTrue(footer.contains("2026-09-02T16:38:22Z"));
		assertTrue(footer.contains("12345678"));
		assertTrue(footer.contains("0.3.2"));
		assertFalse(footer.contains("Private Party Member"));
		assertFalse(footer.contains("Access token secret"));
		assertFalse(footer.contains("/private/path"));
		assertFalse(footer.contains("9999999"));
	}

	@Test
	public void noisyImageIsRecompressedWithinAllLimits()
	{
		BufferedImage noisy = new BufferedImage(1400, 1000, BufferedImage.TYPE_INT_RGB);
		Random random = new Random(7);
		for (int y = 0; y < noisy.getHeight(); y++) for (int x = 0; x < noisy.getWidth(); x++)
			noisy.setRGB(x, y, random.nextInt());
		SubmissionScreenshot shot = ScreenshotCapture.encode(noisy, null, true, record(), "0.3.2");
		assertNotNull(shot);
		assertTrue(shot.width <= 960);
		assertTrue(shot.height <= 720);
		assertTrue(shot.bytes.length <= ScreenshotCapture.MAX_BYTES);
	}

	@Test
	public void missingFrameProducesNoOptionalScreenshot()
	{
		assertNull(ScreenshotCapture.encode(null, null, false, record(), "0.3.2"));
	}

	private static LootRecord record()
	{
		return new LootRecord("12345678-1234-1234-1234-123456789abc", "2026-09-02T16:38:22Z",
			"De Lena", "Barrows", List.of(new LootItem(1, 1, "Item", 500_000)),
			GroupSnapshot.unavailable("none"), SubmissionStatus.LOCAL);
	}

	private static BufferedImage image(int width, int height)
	{
		BufferedImage image = new BufferedImage(width, height, BufferedImage.TYPE_INT_RGB);
		for (int y = 0; y < height; y++)
		{
			for (int x = 0; x < width; x++)
			{
				image.setRGB(x, y, ((x & 255) << 16) | ((y & 255) << 8) | ((x + y) & 255));
			}
		}
		return image;
	}
}
