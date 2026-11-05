"""Phase G: таблица таймеров без дублей и пауза опроса у скрытых окон."""

import unittest

from lamcore import timers


class FakeWindow:
    def __init__(self, mapped=True, state="normal", exists=True):
        self.mapped = mapped
        self._state = state
        self.exists = exists

    def winfo_exists(self):
        return self.exists

    def winfo_ismapped(self):
        return self.mapped

    def state(self):
        return self._state


class TimerTableTest(unittest.TestCase):
    def test_no_duplicate_names(self):
        self.assertEqual(timers.duplicates(), [])

    def test_every_timer_documented(self):
        for item in timers.TIMER_TABLE:
            self.assertTrue(item.name)
            self.assertTrue(item.owner)
            self.assertGreaterEqual(len(item.purpose), 10, item.name)

    def test_no_insane_intervals(self):
        for item in timers.TIMER_TABLE:
            self.assertGreaterEqual(item.interval_ms, 16, item.name)
            self.assertLessEqual(item.interval_ms, 5000, item.name)

    def test_monitor_interval_and_no_cpu_timer(self):
        self.assertEqual(timers.spec("monitor").interval_ms, 2000)
        self.assertNotIn("cpu_process", [item.name for item in timers.TIMER_TABLE],
                         "виджет CPU удалён, его таймера быть не должно")


class ShouldPauseTest(unittest.TestCase):
    def test_normal_window_does_not_pause(self):
        self.assertFalse(timers.should_pause(FakeWindow()))

    def test_minimized_window_pauses(self):
        self.assertTrue(timers.should_pause(FakeWindow(state="iconic")))

    def test_unmapped_window_pauses(self):
        self.assertTrue(timers.should_pause(FakeWindow(mapped=False)))

    def test_destroyed_window_stops(self):
        self.assertTrue(timers.should_pause(FakeWindow(exists=False)))

    def test_explicit_visibility_flag(self):
        self.assertTrue(timers.should_pause(FakeWindow(), visible=False))

    def test_hidden_window_polls_less_often(self):
        self.assertGreater(timers.next_delay("monitor", hidden=True),
                           timers.next_delay("monitor", hidden=False))


if __name__ == "__main__":
    unittest.main()
