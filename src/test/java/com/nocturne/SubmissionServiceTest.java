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
		assertEquals(4, body.get("version").getAsInt());
		assertEquals(record.id, body.get("event_id").getAsString());
		assertFalse(body.toString().contains("PrivateOtherPlayer"));
		assertFalse(body.toString().contains("Bones"));
		assertEquals(526, body.getAsJsonArray("items").get(0).getAsJsonObject().get("item_id").getAsInt());
		assertEquals(32, body.getAsJsonArray("items").get(0).getAsJsonObject().get("unit_price_gp").getAsInt());
		assertEquals("runelite_market", body.getAsJsonArray("items").get(0).getAsJsonObject().get("price_source").getAsString());
	}

	@Test
	public void clientOnlyChambersPolicyDoesNotChangeVersionFourPayload()
	{
		GroupSnapshot group = new GroupSnapshot("Chambers", List.of("PrivateOtherPlayer"), 2,
			GroupSnapshot.Status.MATCHED, "", false, "Below local contribution threshold");
		JsonObject body = SubmissionService.payload(new LootRecord("Simons Alt", "Chambers of Xeric",
			List.of(new LootItem(526, 1, "Bones", 32)), group));
		assertEquals(6, body.entrySet().size());
		assertFalse(body.has("group"));
		assertFalse(body.toString().contains("PrivateOtherPlayer"));
		assertFalse(body.toString().contains("contribution"));
	}

	@Test
	public void payloadContainsConsolidatedStacksInFirstOccurrenceOrder()
	{
		LootRecord record = new LootRecord("Simons Alt", "Man", List.of(
			new LootItem(2361, 2, "Adamantite bar", 1_900),
			new LootItem(526, 1, "Bones", 32),
			new LootItem(2361, 4, "Adamantite bar", 1_900)));

		JsonObject body = SubmissionService.payload(record);
		assertEquals(2, body.getAsJsonArray("items").size());
		assertEquals(2361, body.getAsJsonArray("items").get(0).getAsJsonObject().get("item_id").getAsInt());
		assertEquals(6, body.getAsJsonArray("items").get(0).getAsJsonObject().get("quantity").getAsInt());
		assertEquals(526, body.getAsJsonArray("items").get(1).getAsJsonObject().get("item_id").getAsInt());
	}

	@Test
	public void payloadContainsDerivedValuationMetadata()
	{
		LootItem item = LootItem.derived(29790, 1, "Noxious point", 500_000,
			"runelite_derived_equal_share", "noxious_halberd_components", 1,
			29796, "Noxious halberd", 1_500_001);
		JsonObject entry = SubmissionService.payload(new LootRecord("Simons Alt", "Araxxor", List.of(item)))
			.getAsJsonArray("items").get(0).getAsJsonObject();
		assertEquals("noxious_halberd_components", entry.get("valuation_rule_id").getAsString());
		assertEquals(29796, entry.get("finished_output_item_id").getAsInt());
		assertEquals("Noxious halberd", entry.get("finished_output_item_name").getAsString());
		assertEquals(1_500_001, entry.get("finished_output_market_price_gp").getAsInt());
		assertEquals(500_000, entry.get("derived_unit_price_gp").getAsInt());
	}

	@Test
	public void screenshotPayloadIsVersionFourAndBounded()
	{
		byte[] jpeg = {(byte) 0xff, (byte) 0xd8, 1, 2, (byte) 0xff, (byte) 0xd9};
		SubmissionScreenshot screenshot = new SubmissionScreenshot(
			"image/jpeg", 640, 480, jpeg, "a".repeat(64));
		JsonObject body = SubmissionService.payload(record(), screenshot);
		assertEquals(4, body.get("version").getAsInt());
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
