import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import nginx_reload_convergence as convergence


class NginxReloadConvergenceTest(unittest.TestCase):
    master = 100

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)

    def service(self, *, master=None, active="active", substate="running"):
        master = self.master if master is None else master
        def run(_args, **_kwargs):
            return SimpleNamespace(stdout=(
                "Id=nginx.service\nLoadState=loaded\n"
                f"ActiveState={active}\nSubState={substate}\nMainPID={master}\n"))
        return run

    def generation(self, workers, *, master=None, ambiguous=False):
        master = self.master if master is None else master
        task = self.proc / str(master) / "task" / str(master)
        task.mkdir(parents=True, exist_ok=True)
        (task / "children").write_text(" ".join(map(str, workers)) + "\n")
        for index, pid in enumerate(workers):
            process = self.proc / str(pid); process.mkdir(parents=True, exist_ok=True)
            (process / "stat").write_text(f"{pid} (nginx) S {master} 0 0 0\n")
            command = b"unexpected child" if ambiguous and index == 0 else b"nginx: worker process"
            (process / "cmdline").write_bytes(command + b"\0")

    @staticmethod
    def before():
        return {"schema_version": 1, "purpose": convergence.PURPOSE,
                "master_pid": 100, "worker_pids": [101, 102]}

    def test_immediate_worker_generation_convergence(self):
        self.generation([201, 202])
        result = convergence.wait_for_reload(
            self.before(), run=self.service(), proc_root=self.proc,
            attempts=2, delay=0, sleep=lambda _delay: None)
        self.assertEqual(([201, 202], 1), (result["worker_pids"], result["polls"]))

    def test_delayed_worker_generation_convergence(self):
        self.generation([101, 102])
        sleeps = []
        def advance(delay):
            sleeps.append(delay)
            self.generation([201, 202])
        result = convergence.wait_for_reload(
            self.before(), run=self.service(), proc_root=self.proc,
            attempts=3, delay=0.1, sleep=advance)
        self.assertEqual((2, [0.1]), (result["polls"], sleeps))

    def test_timeout_is_bounded(self):
        self.generation([101, 102])
        sleeps = []
        with self.assertRaises(TimeoutError):
            convergence.wait_for_reload(
                self.before(), run=self.service(), proc_root=self.proc,
                attempts=3, delay=0.1, sleep=sleeps.append)
        self.assertEqual([0.1, 0.1], sleeps)

    def test_master_change_fails_closed(self):
        self.generation([201, 202])
        with self.assertRaisesRegex(RuntimeError, "master changed"):
            convergence.wait_for_reload(
                self.before(), run=self.service(master=999), proc_root=self.proc,
                attempts=1, delay=0)

    def test_nginx_service_failure_fails_closed(self):
        self.generation([201, 202])
        with self.assertRaisesRegex(RuntimeError, "not safely active"):
            convergence.wait_for_reload(
                self.before(), run=self.service(active="failed", substate="failed"),
                proc_root=self.proc, attempts=1, delay=0)

    def test_malformed_or_ambiguous_worker_evidence_fails_closed(self):
        self.generation([201], ambiguous=True)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            convergence.wait_for_reload(
                self.before(), run=self.service(), proc_root=self.proc,
                attempts=1, delay=0)
        (self.proc / str(self.master) / "task" / str(self.master) /
         "children").write_text("201 201\n")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            convergence.wait_for_reload(
                self.before(), run=self.service(), proc_root=self.proc,
                attempts=1, delay=0)

    def test_capture_rejects_master_change_during_evidence_collection(self):
        self.generation([101, 102])
        states = iter((self.master, 999))
        def run(_args, **_kwargs):
            master = next(states)
            return SimpleNamespace(stdout=(
                "Id=nginx.service\nLoadState=loaded\nActiveState=active\n"
                f"SubState=running\nMainPID={master}\n"))
        with self.assertRaisesRegex(RuntimeError, "master changed"):
            convergence.capture_generation(run=run, proc_root=self.proc)


if __name__ == "__main__":
    unittest.main()
