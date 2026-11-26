from pathlib import Path
import subprocess
import tempfile
import unittest

from .release import build, materialize


class Release(unittest.TestCase):
    def test_installs_only_the_pinned_tree_and_detects_drift(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = root/"repo"
        source = repo/"rewrite"
        source.mkdir(parents=True)
        def git(*cmd):
            return subprocess.check_output(["git","-C",repo,"-c","user.name=Fixture","-c","user.email=fixture@example.invalid",
                                            "-c","commit.gpgSign=false",*cmd],text=True).strip()
        git("init","-q")
        for f in ("__init__.py","service.py","agent_tools.py"):
            (source/f).write_text("# test release\n")
        git("add","--","rewrite")
        git("commit","-qm","isolated")
        sha = git("rev-parse","HEAD")
        (source/"service.py").write_text("# not committed\n")
        (source/"other.py").write_text("# not tracked\n")
        release = build(repo,sha,root/"package")
        archive = Path(release["archive"])
        checksum = release["sha256"]
        dest = root/"installed"
        with self.assertRaisesRegex(ValueError,"checksum"):
            materialize(archive,sha,"0"*64,dest,install=True)
        with self.assertRaisesRegex(ValueError,"revision"):
            materialize(archive,"0"*40,checksum,dest,install=True)
        self.assertFalse(dest.exists())
        materialize(archive,sha,checksum,dest,install=True)
        materialize(archive,sha,checksum,dest,install=False)
        self.assertEqual((dest/"rewrite/service.py").read_text(),"# test release\n")
        self.assertFalse((dest/"rewrite/other.py").exists())
        (dest/"extra").touch()
        with self.assertRaisesRegex(ValueError,"file set"):
            materialize(archive,sha,checksum,dest,install=False)
        (dest/"extra").unlink()
        (dest/"rewrite/service.py").write_text("changed")
        with self.assertRaisesRegex(ValueError,"differs"):
            materialize(archive,sha,checksum,dest,install=False)
        with self.assertRaises(FileExistsError):
            materialize(archive,sha,checksum,dest,install=True)
