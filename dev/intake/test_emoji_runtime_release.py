import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import emoji_runtime_release as runtime


class EmojiRuntimeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"runtime"; self.root.mkdir(mode=0o755)
        (self.root/"venvs").mkdir(mode=0o755)
        self.requirements=Path(self.temp.name)/"emoji-sync-requirements.txt"
        self.requirements.write_text(runtime.REQUIREMENTS_TEXT)
        self.wheel=Path(self.temp.name)/runtime.WHEEL_NAME; self.wheel.write_bytes(b"fixture-wheel")
        self.requirements.chmod(0o444); self.wheel.chmod(0o444)
        self.python=Path("/usr/bin/python3.14")
        self.target=self.root/"venvs"/runtime.TARGET_NAME

    def digest(self,path):
        path=Path(path)
        if path==self.wheel: return runtime.WHEEL_SHA256
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def runner(self, host=None):
        calls=[]; host=host or {"version":[3,14],"machine":"x86_64",
            "soabi":"cpython-314-x86_64-linux-gnu","glibc":"glibc 2.43",
            "executable":str(self.python.resolve()),"prefix":str(self.python.resolve().parent.parent)}
        def run(args,**kwargs):
            args=[str(value) for value in args]; calls.append(args)
            if args[0]=="getfacl": return SimpleNamespace(stdout="")
            if args[-2:]==["venv",str(self.target)]:
                bindir=self.target/"bin"; bindir.mkdir()
                (bindir/"python").symlink_to(self.python)
                (bindir/"python3.14").symlink_to(self.python)
                pip=bindir/"pip"; pip.write_text(f"#!{self.target}/bin/python3.14\n"); pip.chmod(0o755)
                (self.target/"lib").mkdir()
                return SimpleNamespace(stdout="")
            joined=" ".join(args)
            if "import json,os,platform" in joined:
                return SimpleNamespace(stdout=json.dumps(host)+"\n")
            if "import json,platform,sys,sysconfig,PIL" in joined:
                value={"version":[3,14],"machine":"x86_64",
                       "soabi":"cpython-314-x86_64-linux-gnu","pillow":"12.3.0",
                       "executable":str(self.python.resolve()),"prefix":str(self.target.resolve())}
                return SimpleNamespace(stdout=json.dumps(value)+"\n")
            return SimpleNamespace(stdout="")
        return run,calls

    def prepare(self,run,**kwargs):
        with patch("emoji_runtime_release.digest",side_effect=self.digest):
            return runtime.prepare(self.root,self.python,self.requirements,self.wheel,
                                   uid=os.getuid(),gid=os.getgid(),run=run,**kwargs)

    def test_architecture_and_abi_fail_before_install_or_target_creation(self):
        for field,value in (("machine","aarch64"),("soabi","cpython-314-aarch64-linux-gnu"),
                            ("version",[3,13]),("glibc","glibc 2.26")):
            host={"version":[3,14],"machine":"x86_64","soabi":runtime.SOABI,
                  "glibc":"glibc 2.43","executable":str(self.python.resolve()),
                  "prefix":"/usr"}; host[field]=value
            run,calls=self.runner(host)
            with self.subTest(field=field),self.assertRaises(ValueError): self.prepare(run,apply=True)
            self.assertFalse(self.target.exists())
            self.assertFalse(any("pip" in call for args in calls for call in args))

    def test_final_path_hash_locked_prepare_check_and_idempotency(self):
        run,calls=self.runner()
        result=self.prepare(run,apply=True)
        self.assertEqual("prepared",result["state"])
        self.assertFalse((self.target/runtime.MARKER).exists())
        self.assertTrue((self.target/runtime.MANIFEST).is_file())
        install=[args for args in calls if "install" in args]
        self.assertEqual(1,len(install)); self.assertIn("--require-hashes",install[0])
        self.assertIn("--only-binary=:all:",install[0]); self.assertIn("--no-index",install[0])
        self.assertFalse(any(".venv-" in value for args in calls for value in args))
        self.assertEqual("already_prepared",self.prepare(run,apply=True)["state"])

    def test_interrupted_runtime_requires_exact_quarantine(self):
        for interrupted in ("after_incomplete_marker", "after_venv_creation",
                            "after_dependency_install", "after_validation"):
            run,_calls=self.runner()
            def fail(phase):
                if phase==interrupted: raise KeyboardInterrupt()
            with self.subTest(phase=interrupted),self.assertRaises(KeyboardInterrupt):
                self.prepare(run,apply=True,fail=fail)
            self.assertTrue((self.target/runtime.MARKER).is_file())
            with self.assertRaisesRegex(ValueError,"exact recovery"):
                self.prepare(run,apply=True)
            checked=runtime.recover_incomplete(
                self.root,self.target,uid=os.getuid(),gid=os.getgid(),run=run)
            self.assertEqual("verified_incomplete",checked["state"])
            moved=runtime.recover_incomplete(
                self.root,self.target,apply=True,uid=os.getuid(),gid=os.getgid(),run=run)
            self.assertFalse(self.target.exists())
            self.assertTrue(Path(moved["quarantine"]).is_dir())

    def test_dependency_record_tamper_and_hard_links_fail_closed(self):
        run,_calls=self.runner(); self.prepare(run,apply=True)
        manifest=self.target/runtime.MANIFEST; manifest.chmod(0o644)
        value=json.loads(manifest.read_text()); value["wheel_sha256"]="0"*64
        manifest.write_text(json.dumps(value)+"\n"); manifest.chmod(0o644)
        with self.assertRaisesRegex(ValueError,"dependency record"):
            runtime.validate_runtime(self.target,uid=os.getuid(),gid=os.getgid(),
                                     approved_python={self.python},run=run)
        manifest.unlink()
        source=self.target/"lib"/"shared"; source.write_text("x")
        os.link(source,self.target/"lib"/"alias")
        with self.assertRaisesRegex(ValueError,"hard-linked"):
            runtime.validate_runtime(self.target,uid=os.getuid(),gid=os.getgid(),
                                     approved_python={self.python},run=run)

    def test_wrong_wheel_digest_fails_before_target_creation(self):
        run,_calls=self.runner()
        with self.assertRaisesRegex(ValueError,"digest mismatch"):
            runtime.prepare(self.root,self.python,self.requirements,self.wheel,apply=True,
                            uid=os.getuid(),gid=os.getgid(),run=run)
        self.assertFalse(self.target.exists())


if __name__=="__main__": unittest.main()
