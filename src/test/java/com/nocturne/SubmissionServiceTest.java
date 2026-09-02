package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.Base64;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.junit.Test;
import static org.junit.Assert.*;

public class SubmissionServiceTest
{
	private LootRecord record()
	{
		return new LootRecord("Simons Alt", "Man", List.of(new LootItem(526, 1, "Bones", 32)),
			new GroupSnapshot("Nearby", List.of("PrivateOtherPlayer"), 0, GroupSnapshot.Status.OBSERVED, ""));
	}

	@Test
	public void payloadContainsOnlyOwnLootAndStableEventId()
	{
		LootRecord record = record();
		JsonObject body = SubmissionService.payload(record);
		assertEquals(6, body.entrySet().size());
		assertEquals(2, body.get("version").getAsInt());
		assertEquals(record.id, body.get("event_id").getAsString());
		assertFalse(body.toString().contains("PrivateOtherPlayer"));
		assertFalse(body.toString().contains("Bones"));
		assertEquals(526, body.getAsJsonArray("items").get(0).getAsJsonObject().get("item_id").getAsInt());
		assertEquals(32, body.getAsJsonArray("items").get(0).getAsJsonObject().get("unit_price_gp").getAsInt());
	}

	@Test
	public void screenshotPayloadIsVersionThreeAndBounded()
	{
		byte[] jpeg = {(byte) 0xff, (byte) 0xd8, 1, 2, (byte) 0xff, (byte) 0xd9};
		SubmissionScreenshot screenshot = new SubmissionScreenshot(
			"image/jpeg", 640, 480, jpeg, "a".repeat(64));
		JsonObject body = SubmissionService.payload(record(), screenshot);
		assertEquals(3, body.get("version").getAsInt());
		JsonObject image = body.getAsJsonObject("screenshot");
		assertEquals("image/jpeg", image.get("mime_type").getAsString());
		assertEquals(640, image.get("width").getAsInt());
		assertArrayEquals(jpeg, Base64.getDecoder().decode(image.get("data_base64").getAsString()));
	}

	@Test
	public void acceptedRequiresMatchingIdAndDevelopmentStorage()
	{
		JsonObject reply = new JsonObject();
		reply.addProperty("event_id", "ours");
		reply.addProperty("status", "stored");
		reply.addProperty("storage", "development");
		assertTrue(SubmissionService.acknowledges(reply, "ours"));
		assertFalse(SubmissionService.acknowledges(reply, "different"));
		reply.addProperty("storage", "production");
		assertFalse(SubmissionService.acknowledges(reply, "ours"));
		assertFalse(SubmissionService.acknowledges(new JsonObject(), "ours"));
	}

	@Test
	public void asyncDeliveryRequiresReceiptAndHandlesErrors() throws Exception
	{
		assertDelivery(201, "receipt", SubmissionStatus.ACCEPTED);
		assertDelivery(200, "<html>Website</html>", SubmissionStatus.UNCERTAIN);
		assertDelivery(400, "{}", SubmissionStatus.REJECTED);
		assertDelivery(503, "{}", SubmissionStatus.UNCERTAIN);
		assertDelivery(200, "x".repeat(2048), SubmissionStatus.UNCERTAIN);
		assertDelivery(0, "", SubmissionStatus.UNCERTAIN);
	}

	private void assertDelivery(int code, String body, SubmissionStatus expected) throws Exception
	{
		LootRecord record = record();
		OkHttpClient http = new OkHttpClient.Builder().addInterceptor(chain ->
		{
			if (code == 0) throw new IOException("Test failure");
			String json = "receipt".equals(body) ? "{\"event_id\":\"" + record.id
				+ "\",\"storage\":\"development\",\"status\":\"stored\"}" : body;
			return new Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
				.code(code).message("Test").body(ResponseBody.create(MediaType.parse("application/json"), json)).build();
		}).build();
		SubmissionService service = new SubmissionService(http, new Gson());
		try
		{
			CountDownLatch done = new CountDownLatch(1);
			List<SubmissionStatus> states = new ArrayList<>();
			service.submit(record, status ->
			{
				states.add(status);
				if (status != SubmissionStatus.SENDING) done.countDown();
			});
			assertTrue(done.await(3, TimeUnit.SECONDS));
			assertEquals(List.of(SubmissionStatus.SENDING, expected), states);
		}
		finally
		{
			service.close();
			http.dispatcher().executorService().shutdownNow();
			http.connectionPool().evictAll();
		}
	}
}
