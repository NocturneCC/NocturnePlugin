package com.nocturne;

import java.awt.Rectangle;
import java.awt.image.BufferedImage;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class ScreenshotCaptureTest
{
	@Test
	public void eligibilityUsesIndividualUnitPrice()
	{
		assertFalse(ScreenshotCapture.isLikelyEligible(List.of(
			new LootItem(1, 2_000_000, "Cheap stack", 1))));
		assertTrue(ScreenshotCapture.isLikelyEligible(List.of(
			new LootItem(2, 1, "Eligible item", 500_000))));
	}

	@Test
	public void croppedJpegIsBoundedAndHasDigest()
	{
		BufferedImage frame = image(1200, 800);
		SubmissionScreenshot shot = ScreenshotCapture.encode(
			frame, new Rectangle(100, 80, 900, 600), false);
		assertNotNull(shot);
		assertEquals("image/jpeg", shot.mimeType);
		assertEquals(900, shot.width);
		assertEquals(600, shot.height);
		assertTrue(shot.bytes.length <= ScreenshotCapture.MAX_BYTES);
		assertEquals(64, shot.sha256.length());
		assertEquals((byte) 0xff, shot.bytes[0]);
		assertEquals((byte) 0xd8, shot.bytes[1]);
	}

	@Test
	public void fullCanvasIsScaledWhenChatIsIncluded()
	{
		SubmissionScreenshot shot = ScreenshotCapture.encode(
			image(1600, 1000), new Rectangle(10, 10, 100, 100), true);
		assertNotNull(shot);
		assertEquals(960, shot.width);
		assertEquals(600, shot.height);
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
