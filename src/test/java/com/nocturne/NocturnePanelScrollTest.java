package com.nocturne;

import javax.swing.JScrollBar;
import org.junit.Test;
import static org.junit.Assert.*;

public class NocturnePanelScrollTest
{
	@Test
	public void prependedLootPreservesOlderEntryViewport()
	{
		JScrollBar scrollBar = scrollBar(140, 30);

		NocturnePanel.restoreViewportAfterPrepend(scrollBar, 30, 100);

		assertEquals(70, scrollBar.getValue());
	}

	@Test
	public void prependedLootDoesNotMoveViewportAtTop()
	{
		JScrollBar scrollBar = scrollBar(140, 0);

		NocturnePanel.restoreViewportAfterPrepend(scrollBar, 0, 100);

		assertEquals(0, scrollBar.getValue());
	}

	@Test
	public void loadingOlderEntriesKeepsCurrentViewportValue()
	{
		JScrollBar scrollBar = scrollBar(300, 90);
		int previousValue = scrollBar.getValue();
		scrollBar.setMaximum(500);
		scrollBar.setValue(previousValue);
		assertEquals(90, scrollBar.getValue());
	}

	private static JScrollBar scrollBar(int maximum, int value)
	{
		JScrollBar scrollBar = new JScrollBar(JScrollBar.VERTICAL);
		scrollBar.setValues(value, 20, 0, maximum);
		return scrollBar;
	}
}
