import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import immutable_runtime_release as runtime


class ImmutableRuntimeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.repo=self.root/"repo"; self.runtime=self.root/"runtime"; self.systemd=self.root/"systemd"
        self.repo.mkdir(); self.runtime.mkdir(mode=0o755); self.systemd.mkdir(mode=0o755)
        subprocess.run(["git","init","-q",str(self.repo)],check=True)
        source=Path(__file__).parent
        intake=self.repo/"dev/intake"; intake.mkdir(parents=True)
        for name in runtime.UNITS: shutil.copyfile(source/name,intake/name)
        (intake/"runtime-requirements.txt").write_text("gunicorn==26.2.0\n")
        shutil.copyfile(source/"emoji-sync-requirements.txt",
                        intake/"emoji-sync-requirements.txt")
        for name in ("nginx-announcements-location.conf", "nginx-emojis-location.conf"):
            shutil.copyfile(source/name,intake/name)
        (self.repo/"payload.py").write_text("committed = True\n")
        self.commit("first")
        self.first=runtime.full_commit(self.repo,"HEAD")
        (self.repo/"payload.py").write_text("committed = False\n")
        self.nginx=self.root/"nocturne"
        announcement=(intake/"nginx-announcements-location.conf").read_text()
        installed="\n".join("    "+line if line else "" for line in announcement.strip().splitlines())+"\n"
        self.nginx.write_text("server {\n    # Nocturne plugin development intake\n"+installed+"}\n")

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
        result=self.prepare(); self.assertEqual("not_prepared",result["state"])
        self.assertFalse((self.runtime/"releases").exists())
        result=self.prepare(apply=True); release=Path(result["release"])
        self.assertEqual("committed = True\n",(release/"payload.py").read_text())
        runtime.verify_release(release,self.first)
        self.assertFalse(release.stat().st_mode & 0o222)
        units=(release/"deployment-units/nocturne-plugin-dev.service").read_text()
        versioned=self.runtime/"venvs"/runtime.VENV_NAME
        self.assertIn(str(self.runtime/"current"),units); self.assertIn(str(versioned),units)
        self.assertIn("/venvs/"+runtime.VENV_NAME+"/bin/python -m gunicorn --bind",units)
        self.assertNotIn("/venv/bin/gunicorn",units)
        self.assertIn('NOCTURNE_TEST_RSNS=Simons Alt,RoatBefAuJu',units)
        source=(Path(__file__).parent/"nocturne-plugin-dev.service").read_text()
        self.assertEqual(source.split("/bin/gunicorn",1)[1],units.split("/bin/python -m gunicorn",1)[1])
        emoji=(release/"deployment-units/nocturne-plugin-emoji-sync.service").read_text()
        self.assertIn(str(self.runtime/"current/dev/intake/emoji_sync.py"),emoji)
        self.assertIn(str(self.runtime/"venvs"/runtime.EMOJI_VENV_NAME/"bin/python"),emoji)
        for name in runtime.UNITS:
            self.assertNotIn("/srv/projects/nocturne-plugin-intake",
                             (release/"deployment-units"/name).read_text())
        self.assertIn("BindReadOnlyPaths=/var/lib/nocturne-plugin-emojis/public:/run/nocturne-plugin-emojis",units)
        self.assertEqual([
            "BindReadOnlyPaths=/srv/projects/nocturne-plugin-announcements-public:/run/nocturne-plugin-announcements",
            "BindReadOnlyPaths=/var/lib/nocturne-plugin-emojis/public:/run/nocturne-plugin-emojis",
        ], [line for line in units.splitlines() if line.startswith("BindReadOnlyPaths=")])
        self.assertNotIn("discord-token",units)
        self.assertNotIn("emoji-sync-config",units)
        for name in runtime.UNITS:
            generated=(release/"deployment-units"/name).read_text()
            if name == "nocturne-plugin-emoji-sync.service":
                self.assertEqual(2,generated.count("LoadCredential="))
            else:
                self.assertNotIn("LoadCredential=",generated)

    def test_operator_script_is_guarded_and_does_not_activate_or_control_services(self):
        text=(Path(__file__).parent/"prepare_immutable_runtime.sh").read_text()
        self.assertTrue(text.startswith("#!/bin/bash\nset -euo pipefail\n"))
        self.assertIn('test "$(id -u)" -ne 0',text)
        for forbidden in ("--activate", "systemctl", "daemon-reload", "sqlite3"):
            self.assertNotIn(forbidden,text)

    def venv_inputs(self):
        release=self.runtime/"release"; (release/"dev/intake").mkdir(parents=True)
        self.runtime.chmod(0o755)
        (release/"dev/intake/intake.py").write_text("")
        (release/"dev/intake/pending_writer.py").write_text("")
        lock=self.runtime/"requirements.lock"; lock.write_text("locked\n")
        wheel=self.runtime/"gunicorn.whl"; wheel.write_text("wheel\n")
        lock.chmod(0o644); wheel.chmod(0o644)
        return release,Path(sys.executable),lock,wheel

    def fake_venv_runner(self,target,python):
        calls=[]
        def run(args,**kwargs):
            args=[str(value) for value in args]; calls.append(args)
            if args[0]=="getfacl": return SimpleNamespace(stdout="")
            if args[-2:]==["venv",str(target)]:
                bindir=target/"bin"; bindir.mkdir()
                (bindir/"python").symlink_to(python)
                launcher=bindir/"gunicorn"; launcher.write_text(f"#!{target}/bin/python\n")
                launcher.chmod(0o755)
                (target/"lib").mkdir(); (target/"lib64").symlink_to("lib",target_is_directory=True)
                return SimpleNamespace(stdout="")
            if args[-3:]==["-m","gunicorn","--version"]:
                return SimpleNamespace(stdout="gunicorn (version 26.2.0)\n")
            if "pathlib.Path(sys.executable).resolve()" in " ".join(args):
                return SimpleNamespace(stdout=f"{python.resolve()}\n{target.resolve()}\n")
            return SimpleNamespace(stdout="")
        return run,calls

    def test_venv_is_created_at_final_path_and_reused(self):
        release,python,lock,wheel=self.venv_inputs()
        target=self.runtime/"venvs"/runtime.VENV_NAME
        run,calls=self.fake_venv_runner(target,python)
        result=runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                    uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual("prepared",result["state"])
        self.assertFalse((target/runtime.VENV_MARKER).exists())
        self.assertTrue((target/runtime.VENV_MANIFEST).is_file())
        self.assertEqual(f"#!{target}/bin/python",(target/"bin/gunicorn").read_text().splitlines()[0])
        venv_calls=[call for call in calls if "venv" in call]
        self.assertEqual(str(target),venv_calls[0][-1])
        self.assertFalse(any(".venv-" in part for call in calls for part in call))
        result=runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                    uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual("already_prepared",result["state"])
        selected=runtime.select_venv(self.runtime,target,apply=True)
        self.assertEqual("selected",selected["state"])
        self.assertEqual(target.resolve(),(self.runtime/"venv").resolve())
        self.assertEqual("already_selected",runtime.select_venv(self.runtime,target)["state"])

    def test_interrupted_preparation_is_marked_and_requires_recovery(self):
        release,python,lock,wheel=self.venv_inputs()
        target=self.runtime/"venvs"/runtime.VENV_NAME
        run,_calls=self.fake_venv_runner(target,python)
        def fail(phase):
            if phase=="after_venv_creation": raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                 uid=os.getuid(),gid=os.getgid(),run=run,fail=fail)
        self.assertTrue((target/runtime.VENV_MARKER).is_file())
        with self.assertRaisesRegex(ValueError,"explicit recovery"):
            runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                 uid=os.getuid(),gid=os.getgid(),run=run)
        checked=runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual("verified_incomplete",checked["state"]); self.assertTrue(target.exists())
        moved=runtime.recover_incomplete_venv(self.runtime,target,apply=True,
                                              uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertFalse(target.exists()); self.assertTrue(Path(moved["quarantine"]).is_dir())
        self.assertEqual("prepared",runtime.prepare_venv(
            self.runtime,release,python,lock,wheel,apply=True,uid=os.getuid(),gid=os.getgid(),run=run)["state"])

    def test_normal_venv_symlinks_are_allowed_but_dangling_and_escaping_are_not(self):
        target=self.runtime/"venvs"/runtime.VENV_NAME; (target/"bin").mkdir(parents=True)
        self.runtime.chmod(0o755); (self.runtime/"venvs").chmod(0o755)
        (target/"lib").mkdir(); (target/"lib64").symlink_to("lib",target_is_directory=True)
        (target/"bin/python").symlink_to(Path(sys.executable))
        target.chmod(0o755); (target/"bin").chmod(0o755); (target/"lib").chmod(0o755)
        run=lambda args,**kwargs: SimpleNamespace(stdout="")
        runtime._validate_venv_tree(target,uid=os.getuid(),gid=os.getgid(),
                                    approved_python={Path(sys.executable)},run=run)
        (target/"bin/escape").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError,"escapes"):
            runtime._validate_venv_tree(target,uid=os.getuid(),gid=os.getgid(),
                                        approved_python={Path(sys.executable)},run=run)
        (target/"bin/escape").unlink(); (target/"bin/dangling").symlink_to("missing")
        with self.assertRaisesRegex(ValueError,"dangling"):
            runtime._validate_venv_tree(target,uid=os.getuid(),gid=os.getgid(),
                                        approved_python={Path(sys.executable)},run=run)

    def test_exact_legacy_renamed_venv_bad_interpreter_can_only_be_quarantined(self):
        target=self.runtime/"venvs"/runtime.VENV_NAME; (target/"bin").mkdir(parents=True)
        self.runtime.chmod(0o755); (self.runtime/"venvs").chmod(0o755)
        (target/"bin/python").symlink_to(Path(sys.executable))
        stale=target.parent/(".venv-"+"a"*40+".1234")/"bin/python"
        launcher=target/"bin/gunicorn"; launcher.write_text(f"#!{stale}\n"); launcher.chmod(0o755)
        target.chmod(0o755); (target/"bin").chmod(0o755)
        run=lambda args,**kwargs: SimpleNamespace(stdout="")
        report=runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual("legacy_renamed_bad_interpreter",report["recovery_kind"])
        launcher.write_text("#!/some/other/missing/python\n")
        with self.assertRaisesRegex(ValueError,"not a verified incomplete"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),run=run)

    def test_recovery_rejects_unknown_target_mount_and_unsafe_acl(self):
        target=self.runtime/"venvs"/runtime.VENV_NAME; target.mkdir(parents=True)
        self.runtime.chmod(0o755); target.parent.chmod(0o755); target.chmod(0o755)
        with self.assertRaisesRegex(ValueError,"legacy Gunicorn launcher"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),
                                            run=lambda args,**kwargs: SimpleNamespace(stdout=""))
        marker=target/runtime.VENV_MARKER
        marker.write_text('{"purpose":"nocturne-runtime-venv-v1","target":"'+str(target)+'"}\n')
        marker.chmod(0o600)
        with patch("immutable_runtime_release.os.path.ismount",side_effect=lambda path: Path(path)==target),\
                self.assertRaisesRegex(ValueError,"mount"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),
                                            run=lambda args,**kwargs: SimpleNamespace(stdout=""))
        def named_acl(args,**kwargs):
            return SimpleNamespace(stdout="user::rwx\nuser:other:r-x\ngroup::r-x\nother::---\n")
        with self.assertRaisesRegex(ValueError,"ACL"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),run=named_acl)

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
        runtime.stage_deployment(self.runtime,self.first,apply=True,
                                 uid=os.getuid(),gid=os.getgid())
        (self.repo/"payload.py").write_text("second = True\n"); self.commit("second")
        second=runtime.full_commit(self.repo,"HEAD"); self.prepare(second,apply=True)
        runtime.stage_deployment(self.runtime,second,apply=True,
                                 uid=os.getuid(),gid=os.getgid())
        self.runtime.mkdir(exist_ok=True); (self.runtime/"current").symlink_to(Path("releases")/self.first)
        for name in runtime.UNITS: (self.systemd/name).write_text("old "+name+"\n")
        return second

    def emoji_runtime_record(self, commit):
        return {"pillow_version": "12.3.0",
                "wheel_sha256": runtime.EMOJI_WHEEL_SHA256,
                "requirements_sha256": runtime.digest(
                    self.runtime/"releases"/commit/"dev/intake/emoji-sync-requirements.txt")}

    def test_activation_failure_restores_symlink_and_units(self):
        phases=("before_activation","after_symlink",
                *(f"after_unit_{index}" for index in range(len(runtime.UNITS))),"after_nginx")
        for phase in phases:
            with self.subTest(phase=phase):
                second=self.two_releases(); before={n:(self.systemd/n).read_bytes() for n in runtime.UNITS}
                def fail(current):
                    if current==phase: raise KeyboardInterrupt()
                with patch.object(runtime,"validate_venv"), \
                        patch("emoji_runtime_release.validate_runtime",
                              return_value=self.emoji_runtime_record(second)), \
                        self.assertRaises(KeyboardInterrupt):
                    runtime.activate(self.runtime,self.systemd,second,nginx_target=self.nginx,
                                     apply=True,fail=fail,
                                     unit_uid=os.getuid(),unit_gid=os.getgid(),
                                     confirmed_services_stopped=True)
                self.assertEqual(self.first,(self.runtime/"current").resolve().name)
                self.assertEqual(before,{n:(self.systemd/n).read_bytes() for n in runtime.UNITS})
                self.tearDown(); self.setUp()

    def test_activation_and_rollback_are_guarded_and_interruption_safe(self):
        second=self.two_releases()
        before_units={name:(self.systemd/name).read_bytes() for name in runtime.UNITS}
        before_nginx=self.nginx.read_bytes()
        with patch.object(runtime,"validate_venv"), \
                patch("emoji_runtime_release.validate_runtime",
                      return_value=self.emoji_runtime_record(second)), \
                self.assertRaisesRegex(ValueError,"must be quiesced"):
            runtime.activate(self.runtime,self.systemd,second,nginx_target=self.nginx,
                             apply=True,unit_uid=os.getuid(),unit_gid=os.getgid())
        with patch.object(runtime,"validate_venv"), \
                patch("emoji_runtime_release.validate_runtime",
                      return_value=self.emoji_runtime_record(second)):
            activated=runtime.activate(self.runtime,self.systemd,second,nginx_target=self.nginx,apply=True,
                                       unit_uid=os.getuid(),unit_gid=os.getgid(),
                                       confirmed_services_stopped=True)
        record=activated["activation_record"]
        applied_units={name:(self.systemd/name).read_bytes() for name in runtime.UNITS}
        applied_nginx=self.nginx.read_bytes()
        self.assertNotEqual(before_nginx,applied_nginx)
        self.assertEqual(self.first,runtime.rollback_activation(
            record,self.runtime,self.systemd,unit_uid=os.getuid(),unit_gid=os.getgid()
        )["restore_commit"])
        phases=("after_rollback_symlink",
                *(f"after_rollback_unit_{index}" for index in range(len(runtime.UNITS))),
                "after_rollback_nginx")
        for phase in phases:
            def fail(current):
                if current==phase: raise KeyboardInterrupt()
            with self.subTest(phase=phase),self.assertRaises(KeyboardInterrupt):
                runtime.rollback_activation(record,self.runtime,self.systemd,apply=True,fail=fail,
                                            unit_uid=os.getuid(),unit_gid=os.getgid(),
                                            confirmed_services_stopped=True)
            self.assertEqual(second,(self.runtime/"current").resolve().name)
            self.assertEqual(applied_units,
                             {name:(self.systemd/name).read_bytes() for name in runtime.UNITS})
            self.assertEqual(applied_nginx,self.nginx.read_bytes())
        result=runtime.rollback_activation(record,self.runtime,self.systemd,apply=True,
                                           unit_uid=os.getuid(),unit_gid=os.getgid(),
                                           confirmed_services_stopped=True)
        self.assertEqual(self.first,result["current_commit"])
        self.assertEqual(before_units,
                         {name:(self.systemd/name).read_bytes() for name in runtime.UNITS})
        self.assertEqual(before_nginx,self.nginx.read_bytes())

    def test_commit_scoped_staging_rejects_tamper_and_mixed_lineage(self):
        self.prepare(apply=True)
        staged=runtime.stage_deployment(self.runtime,self.first,apply=True,
                                        uid=os.getuid(),gid=os.getgid())
        self.assertEqual("prepared",staged["state"])
        runtime.verify_staged_deployment(self.runtime,self.first)
        unit=self.runtime/"staged-units"/self.first/runtime.UNITS[0]
        unit.chmod(0o644); unit.write_text("tampered\n"); unit.chmod(0o444)
        with self.assertRaisesRegex(ValueError,"checksum"):
            runtime.verify_staged_deployment(self.runtime,self.first)

    def test_commit_scoped_route_tamper_fails_closed(self):
        self.prepare(apply=True)
        runtime.stage_deployment(self.runtime,self.first,apply=True,
                                 uid=os.getuid(),gid=os.getgid())
        route=self.runtime/"staged-nginx"/self.first/runtime.EMOJI_ROUTE
        route.chmod(0o644); route.write_text("tampered\n"); route.chmod(0o444)
        with self.assertRaisesRegex(ValueError,"checksum"):
            runtime.verify_staged_deployment(self.runtime,self.first)

    def test_activation_rejects_staged_artifacts_from_another_commit(self):
        second=self.two_releases()
        second_stage=self.runtime/"staged-units"/second
        first_stage=self.runtime/"staged-units"/self.first
        second_stage.chmod(0o755)
        target=second_stage/runtime.UNIT_MANIFEST
        target.chmod(0o644)
        target.write_bytes((first_stage/runtime.UNIT_MANIFEST).read_bytes())
        target.chmod(0o444)
        second_stage.chmod(0o555)
        with self.assertRaisesRegex(ValueError,"lineage"):
            runtime.verify_staged_deployment(self.runtime,second)


if __name__=="__main__": unittest.main()
