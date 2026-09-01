package com.nocturne;

import com.google.inject.Provides;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;
import javax.inject.Inject;
import javax.swing.SwingUtilities;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.GameState;
import net.runelite.api.Player;
import net.runelite.api.events.GameStateChanged;
import net.runelite.api.events.GameTick;
import net.runelite.client.callback.ClientThread;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.events.ConfigChanged;
import net.runelite.client.events.NpcLootReceived;
import net.runelite.client.game.ItemManager;
import net.runelite.client.game.ItemStack;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;
import net.runelite.client.ui.ClientToolbar;
import net.runelite.client.ui.NavigationButton;

@Slf4j
@PluginDescriptor(
	name = "Nocturne",
	description = "View your character and locally detected NPC loot",
	tags = {"nocturne", "clan", "loot"}
)
public class NocturnePlugin extends Plugin
{
	@Inject
	private Client client;

	@Inject
	private ClientThread clientThread;

	@Inject
	private ClientToolbar clientToolbar;

	@Inject
	private ItemManager itemManager;

	@Inject
	private NocturneConfig config;

	// The lifecycle token prevents queued UI work from reviving a disabled plugin.
	private volatile Object lifecycle;
	// Panel and navigation are accessed only on Swing's event dispatch thread.
	private NocturnePanel panel;
	private NavigationButton navigation;

	@Override
	protected void startUp()
	{
		Object token = new Object();
		lifecycle = token;
		SwingUtilities.invokeLater(() ->
		{
			if (lifecycle != token)
			{
				return;
			}
			panel = new NocturnePanel();
			panel.setTracking(config.trackNpcLoot());
			navigation = NavigationButton.builder()
				.tooltip("Nocturne")
				.icon(NocturnePanel.createIcon())
				.priority(6)
				.panel(panel)
				.build();
			clientToolbar.addNavigation(navigation);
			clientThread.invoke(() ->
			{
				if (lifecycle == token)
				{
					updatePlayer();
				}
			});
		});
		log.debug("Nocturne local loot tracker started");
	}

	@Override
	protected void shutDown()
	{
		lifecycle = null;
		SwingUtilities.invokeLater(() ->
		{
			if (navigation != null)
			{
				clientToolbar.removeNavigation(navigation);
				navigation = null;
			}
			panel = null;
		});
		log.debug("Nocturne stopped");
	}

	@Subscribe
	public void onGameTick(GameTick event)
	{
		updatePlayer();
	}

	@Subscribe
	public void onGameStateChanged(GameStateChanged event)
	{
		if (event.getGameState() == GameState.LOGIN_SCREEN)
		{
			withPanel(view -> view.setPlayer(null));
		}
		else if (event.getGameState() == GameState.LOGGED_IN)
		{
			updatePlayer();
		}
	}

	@Subscribe
	public void onConfigChanged(ConfigChanged event)
	{
		if (NocturneConfig.GROUP.equals(event.getGroup()))
		{
			boolean enabled = config.trackNpcLoot();
			withPanel(view -> view.setTracking(enabled));
		}
	}

	@Subscribe
	public void onNpcLootReceived(NpcLootReceived event)
	{
		if (!config.trackNpcLoot() || client.getGameState() != GameState.LOGGED_IN)
		{
			return;
		}
		Player player = client.getLocalPlayer();
		if (player == null || player.getName() == null || event.getItems().isEmpty())
		{
			return;
		}

		// Copy game data on the client thread; never pass NPC or Player objects to Swing.
		String rsn = player.getName();
		String source = event.getNpc() == null ? null : event.getNpc().getName();
		if (source == null || source.isEmpty())
		{
			source = "Unknown NPC";
		}
		List<String> items = new ArrayList<>();
		for (ItemStack item : event.getItems())
		{
			if (item.getQuantity() > 0)
			{
				String name = itemManager.getItemComposition(item.getId()).getName();
				items.add(item.getQuantity() + " x " + name + " [" + item.getId() + "]");
			}
		}
		if (items.isEmpty())
		{
			return;
		}
		LootRecord record = new LootRecord(rsn, source, items);
		withPanel(view -> view.recordLoot(record));
		log.debug("Nocturne detected NPC loot: {} from {}, {} item stacks", rsn, source, items.size());
	}

	private void updatePlayer()
	{
		if (client.getGameState() == GameState.LOGGED_IN)
		{
			Player player = client.getLocalPlayer();
			if (player != null && player.getName() != null)
			{
				String rsn = player.getName();
				withPanel(view -> view.setPlayer(rsn));
			}
		}
	}

	private void withPanel(Consumer<NocturnePanel> action)
	{
		Object token = lifecycle;
		if (token == null)
		{
			return;
		}
		SwingUtilities.invokeLater(() ->
		{
			if (lifecycle == token && panel != null)
			{
				action.accept(panel);
			}
		});
	}

	@Provides
	NocturneConfig provideConfig(ConfigManager manager)
	{
		return manager.getConfig(NocturneConfig.class);
	}
}
