import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import immutable_runtime_release as runtime


class ImmutableRuntimeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.repo=self.root/"repo"; self.runtime=self.root/"runtime"; self.systemd=self.root/"systemd"
        self.repo.mkdir(); self.systemd.mkdir()
        subprocess.run(["git","init","-q",str(self.repo)],check=True)
        source=Path(__file__).parent
        intake=self.repo/"dev/intake"; intake.mkdir(parents=True)
        for name in runtime.UNITS: shutil.copyfile(source/name,intake/name)
        (intake/"runtime-requirements.txt").write_text("gunicorn==26.2.0\n")
        (self.repo/"payload.py").write_text("committed = True\n")
        self.commit("first")
        self.first=runtime.full_commit(self.repo,"HEAD")
        (self.repo/"payload.py").write_text("committed = False\n")

    def tearDown(self):
        if self.root.exists():
            for path in self.root.rglob("*"):
                try: path.chmod(0o755 if path.is_dir() else 0o644)
                except OSError: pass
        self.temp.cleanup()

    def commit(self,message):
        subprocess.run(["git","-C",str(self.repo),"add","."],check=True)
        subprocess.run(["git","-C",str(self.repo),"-c","user.name=Test","-c","user.email=test@example.invalid","commit","-q","-m",message],check=True)

    def prepare(self,commit=None,**kwargs):
        return runtime.prepare(self.repo,self.runtime,commit or self.first,uid=os.getuid(),gid=os.getgid(),**kwargs)

    def test_dry_run_and_archive_uses_committed_object_with_manifest(self):
        result=self.prepare(); self.assertEqual("not_prepared",result["state"]); self.assertFalse(self.runtime.exists())
        result=self.prepare(apply=True); release=Path(result["release"])
        self.assertEqual("committed = True\n",(release/"payload.py").read_text())
        runtime.verify_release(release,self.first)
        self.assertFalse(release.stat().st_mode & 0o222)
        units=(release/"deployment-units/nocturne-plugin-dev.service").read_text()
        self.assertIn(str(self.runtime/"current"),units); self.assertIn(str(self.runtime/"venv"),units)
        self.assertIn('NOCTURNE_TEST_RSNS=Simons Alt,RoatBefAuJu',units)

    def test_prepare_failures_leave_no_release_and_rerun_succeeds(self):
        for phase in ("after_archive","after_manifest","before_release_activation"):
            with self.subTest(phase=phase):
                def fail(current):
                    if current==phase: raise KeyboardInterrupt()
                with self.assertRaises(KeyboardInterrupt): self.prepare(apply=True,fail=fail)
                self.assertFalse((self.runtime/"releases"/self.first).exists())
        self.assertEqual("prepared",self.prepare(apply=True)["state"])
        self.assertEqual("already_prepared",self.prepare(apply=True)["state"])

    def two_releases(self):
        self.prepare(apply=True)
        (self.repo/"payload.py").write_text("second = True\n"); self.commit("second")
        second=runtime.full_commit(self.repo,"HEAD"); self.prepare(second,apply=True)
        self.runtime.mkdir(exist_ok=True); (self.runtime/"current").symlink_to(Path("releases")/self.first)
        for name in runtime.UNITS: (self.systemd/name).write_text("old "+name+"\n")
        return second

    def test_activation_failure_restores_symlink_and_units(self):
        for phase in ("before_activation","after_symlink","after_unit_0","after_unit_1"):
            with self.subTest(phase=phase):
                second=self.two_releases(); before={n:(self.systemd/n).read_bytes() for n in runtime.UNITS}
                def fail(current):
                    if current==phase: raise KeyboardInterrupt()
                with patch.object(runtime,"validate_venv"),self.assertRaises(KeyboardInterrupt):
                    runtime.activate(self.runtime,self.systemd,second,apply=True,fail=fail,
                                     unit_uid=os.getuid(),unit_gid=os.getgid())
                self.assertEqual(self.first,(self.runtime/"current").resolve().name)
                self.assertEqual(before,{n:(self.systemd/n).read_bytes() for n in runtime.UNITS})
                self.tearDown(); self.setUp()

    def test_activation_and_rollback_are_guarded_and_interruption_safe(self):
        second=self.two_releases()
        with patch.object(runtime,"validate_venv"):
            activated=runtime.activate(self.runtime,self.systemd,second,apply=True,
                                       unit_uid=os.getuid(),unit_gid=os.getgid())
        record=activated["activation_record"]
        self.assertEqual(self.first,runtime.rollback_activation(record,self.runtime,self.systemd)["restore_commit"])
        for phase in ("after_rollback_symlink","after_rollback_unit_0","after_rollback_unit_1"):
            def fail(current):
                if current==phase: raise KeyboardInterrupt()
            with self.subTest(phase=phase),self.assertRaises(KeyboardInterrupt):
                runtime.rollback_activation(record,self.runtime,self.systemd,apply=True,fail=fail,
                                            unit_uid=os.getuid(),unit_gid=os.getgid())
            self.assertEqual(second,(self.runtime/"current").resolve().name)
        result=runtime.rollback_activation(record,self.runtime,self.systemd,apply=True,
                                           unit_uid=os.getuid(),unit_gid=os.getgid())
        self.assertEqual(self.first,result["current_commit"])


if __name__=="__main__": unittest.main()
