import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import operator_http_probe as probe


class OperatorHttpProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.state = self.root / probe.STATE_FILENAME
        self.now = 100.0
        self.sleeps = []
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def runner_for(self, statuses):
        remaining = iter(statuses)

        def runner(command, **kwargs):
            self.calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=str(next(remaining)).encode(), stderr=b"private detail")

        return runner

    @staticmethod
    def command():
        return ["/usr/bin/curl", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
                "https://nocturne.events/api/plugin/v1/announcements"]

    def request(self, statuses, **kwargs):
        return probe.request(200, self.command(), self.state, runner=self.runner_for(statuses),
                             clock=self.clock, sleep=self.sleep, **kwargs)

    def test_single_probe_has_no_throttling_delay(self):
        self.assertEqual(200, self.request([200]))
        self.assertEqual([], self.sleeps)
        self.assertEqual(1, len(self.calls))

    def test_sequential_probes_are_paced_without_throttling(self):
        self.assertEqual(200, self.request([200]))
        self.assertEqual(200, self.request([200]))
        self.assertGreaterEqual(self.sleeps[0], probe.MIN_INTERVAL_SECONDS)

    def test_transient_429_is_retried_with_bounded_pacing(self):
        self.assertEqual(200, self.request([429, 200]))
        self.assertEqual(2, len(self.calls))
        self.assertGreaterEqual(self.sleeps[0], probe.MIN_INTERVAL_SECONDS)

    def test_persistent_429_fails_after_bounded_timeout(self):
        with self.assertRaisesRegex(probe.ProbeFailure, "deadline exceeded"):
            self.request([429, 429, 429], max_429_retries=5, timeout=1.0)
        self.assertEqual(2, len(self.calls))
        self.assertLessEqual(self.now, 101.0)

    def test_unexpected_non_429_status_fails_immediately(self):
        with self.assertRaisesRegex(probe.ProbeFailure, "unexpected HTTP status 403"):
            self.request([403])
        self.assertEqual(1, len(self.calls))

    def test_transport_and_malformed_status_errors_are_sanitized(self):
        def failed(*args, **kwargs):
            raise OSError("secret-bearing URL and body must not escape")

        with self.assertRaisesRegex(probe.ProbeFailure, "transport failed") as caught:
            probe.request(200, self.command(), self.state, runner=failed,
                          clock=self.clock, sleep=self.sleep)
        self.assertNotIn("secret-bearing", str(caught.exception))

        bad = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"200body", stderr=b"raw")
        with self.assertRaisesRegex(probe.ProbeFailure, "status output is malformed"):
            probe.request(200, self.command(), self.state, runner=bad,
                          clock=self.clock, sleep=self.sleep)

    def test_state_file_metadata_and_schema_fail_closed(self):
        self.state.write_text(json.dumps({"last_request_monotonic_ns": True}))
        self.state.chmod(0o600)
        with self.assertRaisesRegex(probe.ProbeFailure, "schema is invalid"):
            self.request([200])

    def test_state_file_is_private_and_bounded(self):
        self.request([200])
        self.assertEqual(0o600, self.state.stat().st_mode & 0o777)
        self.assertEqual(1, self.state.stat().st_nlink)
        self.assertEqual({"last_request_monotonic_ns"}, json.loads(self.state.read_text()).keys())


if __name__ == "__main__":
    unittest.main()
