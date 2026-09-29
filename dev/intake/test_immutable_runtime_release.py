import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import immutable_runtime_release as runtime
import runtime_identity


class ImmutableRuntimeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.repo=self.root/"repo"; self.runtime=self.root/"runtime"; self.systemd=self.root/"systemd"
        self.repo.mkdir(); self.runtime.mkdir(mode=0o755); self.systemd.mkdir(mode=0o755)
        subprocess.run(["git","init","-q",str(self.repo)],check=True)
        source=Path(__file__).parent
        intake=self.repo/"dev/intake"; intake.mkdir(parents=True)
        for name in runtime.UNITS: shutil.copyfile(source/name,intake/name)
        (intake/"runtime-requirements.lock").write_text(runtime.GUNICORN_LOCK_TEXT)
        shutil.copyfile(source/"emoji-sync-requirements.txt",
                        intake/"emoji-sync-requirements.txt")
        for name in ("nginx-announcements-location.conf", "nginx-emojis-location.conf"):
            shutil.copyfile(source/name,intake/name)
        (self.repo/"payload.py").write_text("committed = True\n")
        self.commit("first")
        self.first=runtime.full_commit(self.repo,"HEAD")
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
        self.assertIn("BindReadOnlyPaths=-/var/lib/nocturne-plugin-emojis/public:/run/nocturne-plugin-emojis",units)
        self.assertIn("InaccessiblePaths=/var/lib/nocturne-plugin-emojis",units)
        self.assertIn("InaccessiblePaths=/etc/nocturne-plugin/emoji-sync.json",units)
        self.assertIn("InaccessiblePaths=/etc/nocturne-plugin/credentials",units)
        self.assertEqual([
            "BindReadOnlyPaths=/srv/projects/nocturne-plugin-announcements-public:/run/nocturne-plugin-announcements",
            "BindReadOnlyPaths=-/var/lib/nocturne-plugin-emojis/public:/run/nocturne-plugin-emojis",
        ], [line for line in units.splitlines() if line.startswith("BindReadOnlyPaths=")])
        self.assertNotIn("discord-token",units)
        self.assertNotIn("emoji-sync-config",units)
        for name in runtime.UNITS:
            generated=(release/"deployment-units"/name).read_text()
            if name == "nocturne-plugin-emoji-sync.service":
                self.assertEqual(2,generated.count("LoadCredential="))
            else:
                self.assertNotIn("LoadCredential=",generated)
        self.assertNotIn("/venvs/" + runtime.LEGACY_VENV_NAME + "/", units)

    def test_dependency_lock_changes_create_distinct_runtime_identities(self):
        source=Path(__file__).parent
        self.assertEqual(runtime.GUNICORN_LOCK_TEXT,
                         (source/"runtime-requirements.lock").read_text())
        self.assertEqual(runtime.PILLOW_LOCK_SHA256,
                         runtime.digest(source/"emoji-sync-requirements.txt"))
        other_lock = runtime_identity.lock_digest(
            runtime_identity.GUNICORN_LOCK_TEXT + "# provenance revision\n")
        other_name = runtime_identity.content_addressed_name(
            runtime.LEGACY_VENV_NAME, other_lock)
        self.assertNotEqual(runtime.VENV_NAME, other_name)
        self.assertTrue(runtime.VENV_NAME.endswith(
            runtime.GUNICORN_LOCK_SHA256[:runtime_identity.IDENTITY_DIGEST_LENGTH]))
        self.assertTrue(other_name.endswith(
            other_lock[:runtime_identity.IDENTITY_DIGEST_LENGTH]))
        self.assertEqual(runtime.GUNICORN_VERSION, "26.2.0")

    def test_legacy_runtime_uses_its_pinned_historical_record(self):
        target=self.runtime/"venvs"/runtime.LEGACY_VENV_NAME
        (target/"bin").mkdir(parents=True)
        (target/"bin/python").symlink_to("/usr/bin/python3.14")
        manifest={
            "purpose": runtime.LEGACY_VENV_PURPOSE,
            "target": str(target),
            "python": "/usr/bin/python3.14",
            "requirements_sha256": runtime.LEGACY_GUNICORN_LOCK_SHA256,
            "wheel_sha256": runtime.GUNICORN_WHEEL_SHA256,
            "gunicorn_version": runtime.GUNICORN_VERSION,
        }
        (target/runtime.VENV_MANIFEST).write_text(json.dumps(manifest)+"\n")
        probe={"version":runtime.PYTHON_VERSION,"machine":runtime.PYTHON_MACHINE,
               "soabi":runtime.PYTHON_SOABI,"executable":"/usr/bin/python3.14",
               "prefix":str(target.resolve()),
               "packages":[["gunicorn",runtime.GUNICORN_VERSION],
                           ["pip","25.1.1"]]}
        def run(args,**_kwargs):
            if args[-3:]==["-m","gunicorn","--version"]:
                return SimpleNamespace(stdout="gunicorn (version 26.2.0)\n")
            return SimpleNamespace(stdout="")
        with patch.object(runtime,"_validate_venv_tree"), \
                patch.object(runtime,"_runtime_probe",return_value=probe), \
                patch.object(runtime,"_validate_runtime_launchers"):
            self.assertEqual(runtime.LEGACY_GUNICORN_LOCK_SHA256,
                runtime.validate_legacy_venv(target,uid=os.getuid(),gid=os.getgid(),
                                             run=run)["requirements_sha256"])
            manifest["requirements_sha256"]="0"*64
            (target/runtime.VENV_MANIFEST).write_text(json.dumps(manifest)+"\n")
            with self.assertRaisesRegex(ValueError,"dependency record"):
                runtime.validate_legacy_venv(target,uid=os.getuid(),gid=os.getgid(),run=run)

    def test_prepare_rejects_dirty_checkout_wrong_or_abbreviated_commit(self):
        (self.repo/"payload.py").write_text("dirty = True\n")
        with self.assertRaisesRegex(ValueError,"dirty"):
            self.prepare()
        subprocess.run(["git","-C",str(self.repo),"restore","payload.py"],check=True)
        with self.assertRaisesRegex(ValueError,"exact full"):
            runtime.prepare(self.repo,self.runtime,self.first[:12],uid=os.getuid(),gid=os.getgid())
        with self.assertRaisesRegex(ValueError,"HEAD"):
            runtime.prepare(self.repo,self.runtime,"0"*40,uid=os.getuid(),gid=os.getgid())

    def test_operator_script_is_guarded_and_does_not_activate_or_control_services(self):
        text=(Path(__file__).parent/"prepare_immutable_runtime.sh").read_text()
        self.assertTrue(text.startswith("#!/bin/bash\nset -euo pipefail\n"))
        self.assertIn('effective_uid=$(id -u 2>&1)',text)
        for forbidden in ("--activate", "systemctl", "daemon-reload", "sqlite3"):
            self.assertNotIn(forbidden,text)
        self.assertLess(text.index("--host-preflight"),text.index(' --prepare\n'))
        self.assertIn("wheelhouse/"+runtime.VENV_NAME+"/"+runtime.GUNICORN_WHEEL_NAME,text)
        self.assertIn("venvs/"+runtime.VENV_NAME,text)
        self.assertIn("wheelhouse/"+runtime.EMOJI_VENV_NAME,text)
        self.assertIn("venvs/"+runtime.EMOJI_VENV_NAME,text)
        self.assertNotIn('target="$root/venvs/'+runtime.LEGACY_VENV_NAME+'"',text)

    def test_service_state_verifier_requires_exact_inactive_systemd_evidence(self):
        def runner(args,**_kwargs):
            name=args[2]
            return SimpleNamespace(stdout=(f"Id={name}\nLoadState=loaded\n"
                "ActiveState=inactive\nSubState=dead\nMainPID=0\n"))
        self.assertEqual(set(runtime.SERVICE_UNITS),set(runtime.verify_inactive_services(runner)))
        def active(args,**_kwargs):
            name=args[2]
            return SimpleNamespace(stdout=(f"Id={name}\nLoadState=loaded\n"
                "ActiveState=active\nSubState=running\nMainPID=123\n"))
        with self.assertRaisesRegex(ValueError,"not inactive"):
            runtime.verify_inactive_services(active)

    def venv_inputs(self):
        release=self.runtime/"releases"/("f"*40); (release/"dev/intake").mkdir(parents=True)
        self.runtime.chmod(0o755)
        (release/"dev/intake/intake.py").write_text("")
        (release/"dev/intake/pending_writer.py").write_text("")
        lock=release/"dev/intake/runtime-requirements.lock"
        lock.write_text(runtime.GUNICORN_LOCK_TEXT); lock.chmod(0o444)
        wheelhouse=self.runtime/"wheelhouse"/runtime.VENV_NAME
        wheelhouse.mkdir(parents=True); (self.runtime/"wheelhouse").chmod(0o755); wheelhouse.chmod(0o755)
        wheel=wheelhouse/runtime.GUNICORN_WHEEL_NAME; wheel.write_text("wheel\n")
        wheel.chmod(0o444)
        return release,Path(sys.executable),lock,wheel

    def wheel_digest(self, wheel):
        original=runtime.digest
        return patch.object(runtime,"digest",side_effect=lambda value:
            runtime.GUNICORN_WHEEL_SHA256 if Path(value)==wheel else original(value))

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
                pip=bindir/"pip"; pip.write_text(f"#!{target}/bin/python\n")
                pip.chmod(0o755)
                (target/"lib").mkdir(); (target/"lib64").symlink_to("lib",target_is_directory=True)
                return SimpleNamespace(stdout="")
            if args[-3:]==["-m","gunicorn","--version"]:
                return SimpleNamespace(stdout="gunicorn (version 26.2.0)\n")
            if "importlib.metadata" in " ".join(args):
                return SimpleNamespace(stdout=json.dumps({
                    "version": runtime.PYTHON_VERSION,
                    "machine": runtime.PYTHON_MACHINE,
                    "soabi": runtime.PYTHON_SOABI,
                    "executable": str(python.resolve()),
                    "prefix": str(target.resolve()),
                    "packages": [["gunicorn", runtime.GUNICORN_VERSION],
                                 ["pip", "25.1.1"]],
                }) + "\n")
            return SimpleNamespace(stdout="")
        return run,calls

    def test_venv_is_created_at_final_path_and_reused(self):
        release,python,lock,wheel=self.venv_inputs()
        target=self.runtime/"venvs"/runtime.VENV_NAME
        selector=self.runtime/"venv"
        selector.symlink_to(Path("venvs")/runtime.LEGACY_VENV_NAME)
        original_selector=selector.readlink()
        run,calls=self.fake_venv_runner(target,python)
        with self.wheel_digest(wheel),patch.object(runtime,"_system_python",return_value=python.resolve()):
            result=runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                        uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual("prepared",result["state"])
        self.assertFalse((target/runtime.VENV_MARKER).exists())
        self.assertTrue((target/runtime.VENV_MANIFEST).is_file())
        self.assertEqual(f"#!{target}/bin/python",(target/"bin/gunicorn").read_text().splitlines()[0])
        venv_calls=[call for call in calls if "venv" in call]
        self.assertEqual(str(target),venv_calls[0][-1])
        self.assertFalse(any(".venv-" in part for call in calls for part in call))
        with self.wheel_digest(wheel),patch.object(runtime,"_system_python",return_value=python.resolve()):
            result=runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                        uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual("already_prepared",result["state"])
        self.assertEqual(original_selector,selector.readlink())
        self.assertNotEqual(runtime.LEGACY_VENV_NAME,runtime.VENV_NAME)

    def test_interrupted_preparation_is_marked_and_requires_recovery(self):
        release,python,lock,wheel=self.venv_inputs()
        target=self.runtime/"venvs"/runtime.VENV_NAME
        run,_calls=self.fake_venv_runner(target,python)
        def fail(phase):
            if phase=="after_venv_creation": raise KeyboardInterrupt()
        with self.wheel_digest(wheel),patch.object(runtime,"_system_python",return_value=python.resolve()),self.assertRaises(KeyboardInterrupt):
            runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                 uid=os.getuid(),gid=os.getgid(),run=run,fail=fail)
        self.assertTrue((target/runtime.VENV_MARKER).is_file())
        with self.wheel_digest(wheel),patch.object(runtime,"_system_python",return_value=python.resolve()),self.assertRaisesRegex(ValueError,"explicit recovery"):
            runtime.prepare_venv(self.runtime,release,python,lock,wheel,apply=True,
                                 uid=os.getuid(),gid=os.getgid(),run=run)
        checked=runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual("verified_incomplete",checked["state"]); self.assertTrue(target.exists())
        moved=runtime.recover_incomplete_venv(self.runtime,target,apply=True,
                                              uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertFalse(target.exists()); self.assertTrue(Path(moved["quarantine"]).is_dir())
        with self.wheel_digest(wheel),patch.object(runtime,"_system_python",return_value=python.resolve()):
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

    def test_unmarked_or_legacy_renamed_target_cannot_be_quarantined(self):
        target=self.runtime/"venvs"/runtime.VENV_NAME; (target/"bin").mkdir(parents=True)
        self.runtime.chmod(0o755); (self.runtime/"venvs").chmod(0o755)
        (target/"bin/python").symlink_to(Path(sys.executable))
        stale=target.parent/(".venv-"+"a"*40+".1234")/"bin/python"
        launcher=target/"bin/gunicorn"; launcher.write_text(f"#!{stale}\n"); launcher.chmod(0o755)
        target.chmod(0o755); (target/"bin").chmod(0o755)
        run=lambda args,**kwargs: SimpleNamespace(stdout="")
        with self.assertRaisesRegex(ValueError,"incomplete marker"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),run=run)

    def test_recovery_rejects_unknown_target_mount_and_unsafe_acl(self):
        target=self.runtime/"venvs"/runtime.VENV_NAME; target.mkdir(parents=True)
        self.runtime.chmod(0o755); target.parent.chmod(0o755); target.chmod(0o755)
        with self.assertRaisesRegex(ValueError,"incomplete marker"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),
                                            run=lambda args,**kwargs: SimpleNamespace(stdout=""))
        marker=target/runtime.VENV_MARKER
        marker.write_text(json.dumps({"purpose": runtime.VENV_PURPOSE,
                                      "target": str(target)}) + "\n")
        marker.chmod(0o600)
        with patch("immutable_runtime_release.os.path.ismount",side_effect=lambda path: Path(path)==target),\
                self.assertRaisesRegex(ValueError,"mount"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),
                                            run=lambda args,**kwargs: SimpleNamespace(stdout=""))
        def named_acl(args,**kwargs):
            return SimpleNamespace(stdout="user::rwx\nuser:other:r-x\ngroup::r-x\nother::---\n")
        with self.assertRaisesRegex(ValueError,"ACL"):
            runtime.recover_incomplete_venv(self.runtime,target,uid=os.getuid(),gid=os.getgid(),run=named_acl)

    def test_incomplete_runtime_quarantine_reservation_is_collision_safe(self):
        target=self.runtime/"venvs"/runtime.VENV_NAME
        target.mkdir(parents=True)
        self.runtime.chmod(0o755); target.parent.chmod(0o755); target.chmod(0o755)
        marker=target/runtime.VENV_MARKER
        marker.write_text(json.dumps({"purpose":runtime.VENV_PURPOSE,
                                      "target":str(target)})+"\n")
        marker.chmod(0o600)
        quarantine=self.runtime/"quarantine/incomplete-venvs"
        quarantine.mkdir(parents=True,mode=0o700)
        (self.runtime/"quarantine").chmod(0o700); quarantine.chmod(0o700)
        collision=quarantine/(runtime.VENV_NAME+"-collision")
        collision.mkdir(mode=0o700)
        run=lambda args,**kwargs: SimpleNamespace(stdout="")
        identities=[SimpleNamespace(hex="collision"),SimpleNamespace(hex="unique")]
        with patch("immutable_runtime_release.uuid4",side_effect=identities):
            moved=runtime.recover_incomplete_venv(
                self.runtime,target,apply=True,uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertEqual(quarantine/(runtime.VENV_NAME+"-unique")/"runtime",
                         Path(moved["quarantine"]))
        self.assertTrue(collision.is_dir())

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
        for name in runtime.UNITS:
            shutil.copyfile(self.runtime/"releases"/self.first/"deployment-units"/name,
                            self.systemd/name)
        return second

    def emoji_runtime_record(self, commit):
        return {"schema_version": 2,
                "purpose": "nocturne-emoji-runtime-v2",
                "pillow_version": "12.3.0",
                "python_version": runtime.PYTHON_VERSION,
                "soabi": runtime.PYTHON_SOABI,
                "machine": runtime.PYTHON_MACHINE,
                "wheel_sha256": runtime.EMOJI_WHEEL_SHA256,
                "requirements_sha256": runtime.digest(
                    self.runtime/"releases"/commit/"dev/intake/emoji-sync-requirements.txt")}

    def core_runtime_record(self, commit):
        return {"schema_version": 2,
                "purpose": runtime.VENV_PURPOSE,
                "gunicorn_version": runtime.GUNICORN_VERSION,
                "python_version": runtime.PYTHON_VERSION,
                "soabi": runtime.PYTHON_SOABI,
                "machine": runtime.PYTHON_MACHINE,
                "wheel_sha256": runtime.GUNICORN_WHEEL_SHA256,
                "requirements_sha256": runtime.digest(
                    self.runtime/"releases"/commit/"dev/intake/runtime-requirements.lock")}

    def service_evidence(self):
        return {name:{"Id":name,"LoadState":"loaded","ActiveState":"inactive",
                      "SubState":"dead","MainPID":"0"} for name in runtime.SERVICE_UNITS}

    def activate(self, commit, *, fail=None):
        kwargs={"nginx_target":self.nginx,"unit_uid":os.getuid(),"unit_gid":os.getgid(),
                "service_state_verifier":self.service_evidence}
        with patch.object(runtime,"validate_venv",return_value=self.core_runtime_record(commit)), \
                patch("emoji_runtime_release.validate_runtime",
                      return_value=self.emoji_runtime_record(commit)):
            dry=runtime.activate(self.runtime,self.systemd,commit,**kwargs)
            return runtime.activate(self.runtime,self.systemd,commit,apply=True,fail=fail,
                                    confirmed_services_stopped=True,
                                    expected_prestate_sha256=dry["prestate_sha256"],**kwargs)

    def test_activation_failure_restores_symlink_and_units(self):
        phases=("after_activation_record","before_activation","after_symlink",
                *(f"after_unit_{index}" for index in range(len(runtime.UNITS))),"after_nginx")
        for phase in phases:
            with self.subTest(phase=phase):
                second=self.two_releases(); before={n:(self.systemd/n).read_bytes() for n in runtime.UNITS}
                def fail(current):
                    if current==phase: raise KeyboardInterrupt()
                with self.assertRaises(KeyboardInterrupt):
                    self.activate(second,fail=fail)
                self.assertEqual(self.first,(self.runtime/"current").resolve().name)
                self.assertEqual(before,{n:(self.systemd/n).read_bytes() for n in runtime.UNITS})
                self.tearDown(); self.setUp()

    def test_activation_and_rollback_are_guarded_and_interruption_safe(self):
        second=self.two_releases()
        before_units={name:(self.systemd/name).read_bytes() for name in runtime.UNITS}
        before_nginx=self.nginx.read_bytes()
        with patch.object(runtime,"validate_venv",return_value=self.core_runtime_record(second)), \
                patch("emoji_runtime_release.validate_runtime",
                      return_value=self.emoji_runtime_record(second)), \
                self.assertRaisesRegex(ValueError,"must be quiesced"):
            runtime.activate(self.runtime,self.systemd,second,nginx_target=self.nginx,
                             apply=True,unit_uid=os.getuid(),unit_gid=os.getgid(),
                             service_state_verifier=self.service_evidence)
        activated=self.activate(second)
        record=activated["activation_record"]
        applied_units={name:(self.systemd/name).read_bytes() for name in runtime.UNITS}
        applied_nginx=self.nginx.read_bytes()
        self.assertNotEqual(before_nginx,applied_nginx)
        self.assertEqual(self.first,runtime.rollback_activation(
            record,self.runtime,self.systemd,unit_uid=os.getuid(),unit_gid=os.getgid(),
            service_state_verifier=self.service_evidence
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
                                            confirmed_services_stopped=True,
                                            service_state_verifier=self.service_evidence)
            self.assertEqual(second,(self.runtime/"current").resolve().name)
            self.assertEqual(applied_units,
                             {name:(self.systemd/name).read_bytes() for name in runtime.UNITS})
            self.assertEqual(applied_nginx,self.nginx.read_bytes())
        result=runtime.rollback_activation(record,self.runtime,self.systemd,apply=True,
                                           unit_uid=os.getuid(),unit_gid=os.getgid(),
                                           confirmed_services_stopped=True,
                                           service_state_verifier=self.service_evidence)
        self.assertEqual(self.first,result["current_commit"])
        self.assertEqual(before_units,
                         {name:(self.systemd/name).read_bytes() for name in runtime.UNITS})
        self.assertEqual(before_nginx,self.nginx.read_bytes())
        repeated=runtime.rollback_activation(
            record,self.runtime,self.systemd,apply=True,unit_uid=os.getuid(),unit_gid=os.getgid(),
            confirmed_services_stopped=True,service_state_verifier=self.service_evidence)
        self.assertTrue(repeated["already_restored"])

    def test_rollback_restores_legacy_runtime_unit_paths_exactly(self):
        second=self.two_releases()
        for name in runtime.CORE_UNITS:
            predecessor=self.runtime/"releases"/self.first/"deployment-units"/name
            predecessor.chmod(0o644)
            predecessor.write_text(predecessor.read_text().replace(
                runtime.VENV_NAME, runtime.LEGACY_VENV_NAME))
            target=self.systemd/name
            target.write_text(target.read_text().replace(
                runtime.VENV_NAME, runtime.LEGACY_VENV_NAME))
        predecessor_manifest=self.runtime/"releases"/self.first/"RELEASE-MANIFEST.json"
        predecessor_manifest.chmod(0o644)
        runtime.build_manifest(self.runtime/"releases"/self.first,self.first)
        runtime._make_read_only(self.runtime/"releases"/self.first,
                                os.getuid(),os.getgid())
        before={name:(self.systemd/name).read_bytes() for name in runtime.UNITS}
        activated=self.activate(second)
        runtime.rollback_activation(
            activated["activation_record"],self.runtime,self.systemd,apply=True,
            unit_uid=os.getuid(),unit_gid=os.getgid(),
            confirmed_services_stopped=True,
            service_state_verifier=self.service_evidence)
        self.assertEqual(before,{name:(self.systemd/name).read_bytes()
                                 for name in runtime.UNITS})
        for name in runtime.CORE_UNITS:
            self.assertIn(runtime.LEGACY_VENV_NAME,
                          (self.systemd/name).read_text())

    def test_repeated_activation_is_idempotent_only_when_every_artifact_matches(self):
        second=self.two_releases(); self.activate(second)
        kwargs={"nginx_target":self.nginx,"unit_uid":os.getuid(),"unit_gid":os.getgid(),
                "service_state_verifier":self.service_evidence}
        with patch.object(runtime,"validate_venv",return_value=self.core_runtime_record(second)), \
                patch("emoji_runtime_release.validate_runtime",
                      return_value=self.emoji_runtime_record(second)):
            self.assertEqual("already_active",runtime.activate(
                self.runtime,self.systemd,second,apply=True,**kwargs)["state"])
            target=self.systemd/runtime.UNITS[0]; target.write_text("tampered\n")
            with self.assertRaisesRegex(ValueError,"mismatched units"):
                runtime.activate(self.runtime,self.systemd,second,**kwargs)

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

    def test_staging_manifests_bind_commit_metadata_purpose_and_exact_files(self):
        self.prepare(apply=True)
        runtime.stage_deployment(self.runtime,self.first,apply=True,
                                 uid=os.getuid(),gid=os.getgid())
        for directory,manifest_name,purpose in (
                (self.runtime/"staged-units"/self.first,runtime.UNIT_MANIFEST,
                 runtime.UNIT_STAGE_PURPOSE),
                (self.runtime/"staged-nginx"/self.first,runtime.ROUTE_MANIFEST,
                 runtime.ROUTE_STAGE_PURPOSE)):
            manifest=json.loads((directory/manifest_name).read_text())
            self.assertEqual((self.first,purpose),(manifest["commit"],manifest["purpose"]))
            self.assertEqual({"uid":os.getuid(),"gid":os.getgid(),"mode":"0555","acl":"basic"},
                             manifest["directory"])
            directory.chmod(0o755); (directory/"unexpected").write_text("x"); directory.chmod(0o555)
            with self.assertRaisesRegex(ValueError,"file set"):
                runtime.verify_staged_deployment(self.runtime,self.first)
            directory.chmod(0o755); (directory/"unexpected").unlink(); directory.chmod(0o555)

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

    def test_prepared_activation_record_recovers_a_mixed_crash_state(self):
        second=self.two_releases(); activated=self.activate(second)
        record=Path(activated["activation_record"]); state_path=record/"ACTIVATION.json"
        state=json.loads(state_path.read_text()); state["status"]="prepared"
        state_path.write_text(json.dumps(state,sort_keys=True)+"\n"); state_path.chmod(0o600)
        replacement=self.runtime/".fixture-current"
        replacement.symlink_to(Path("releases")/self.first)
        os.replace(replacement,self.runtime/"current")
        first_entry=state["units"][0]
        Path(first_entry["target"]).write_bytes((record/first_entry["backup"]).read_bytes())
        dry=runtime.recover_interrupted_activation(
            record,self.runtime,self.systemd,nginx_target=self.nginx,
            unit_uid=os.getuid(),unit_gid=os.getgid(),
            service_state_verifier=self.service_evidence)
        recovered=runtime.recover_interrupted_activation(
            record,self.runtime,self.systemd,nginx_target=self.nginx,apply=True,
            unit_uid=os.getuid(),unit_gid=os.getgid(),confirmed_services_stopped=True,
            expected_recovery_sha256=dry["recovery_sha256"],
            service_state_verifier=self.service_evidence)
        self.assertEqual("restored",recovered["state"])
        self.assertEqual(self.first,(self.runtime/"current").resolve().name)
        self.assertEqual("restored",json.loads(state_path.read_text())["status"])

    def test_interrupted_rollback_record_recovers_applied_state(self):
        second=self.two_releases(); activated=self.activate(second)
        record=Path(activated["activation_record"])
        def fail(phase):
            if phase=="after_rollback_record_prepared": raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            runtime.rollback_activation(
                record,self.runtime,self.systemd,nginx_target=self.nginx,apply=True,fail=fail,
                unit_uid=os.getuid(),unit_gid=os.getgid(),confirmed_services_stopped=True,
                service_state_verifier=self.service_evidence)
        self.assertEqual("rollback_prepared",json.loads(
            (record/"ACTIVATION.json").read_text())["status"])
        dry=runtime.recover_interrupted_activation(
            record,self.runtime,self.systemd,nginx_target=self.nginx,
            unit_uid=os.getuid(),unit_gid=os.getgid(),
            service_state_verifier=self.service_evidence)
        result=runtime.recover_interrupted_activation(
            record,self.runtime,self.systemd,nginx_target=self.nginx,apply=True,
            unit_uid=os.getuid(),unit_gid=os.getgid(),confirmed_services_stopped=True,
            expected_recovery_sha256=dry["recovery_sha256"],
            service_state_verifier=self.service_evidence)
        self.assertEqual("applied",result["state"])
        self.assertEqual(second,(self.runtime/"current").resolve().name)

    def test_predecessor_without_emoji_units_is_supported(self):
        second=self.two_releases(); previous=self.runtime/"releases"/self.first
        for path in [previous,*previous.rglob("*")]:
            if not path.is_symlink(): path.chmod(0o755 if path.is_dir() else 0o644)
        for name in runtime.EMOJI_UNITS:
            (previous/"deployment-units"/name).unlink()
            (self.systemd/name).unlink()
        runtime.build_manifest(previous,self.first)
        runtime._make_read_only(previous,os.getuid(),os.getgid())
        self.nginx.write_text("server {\n    # Nocturne plugin development intake\n}\n")
        activated=self.activate(second)
        self.assertEqual(second,(self.runtime/"current").resolve().name)
        self.assertIn("/api/plugin/v1/announcements",self.nginx.read_text())
        self.assertIn("/api/plugin/v1/emojis",self.nginx.read_text())
        self.assertTrue(Path(activated["activation_record"]).is_dir())


if __name__=="__main__": unittest.main()
