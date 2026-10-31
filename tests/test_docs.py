"""The docs stay true: every command shown parses, every flag exists, every relative link resolves.

Offline and cheap: each command shown in a ```bash block of README.md or docs/*.md is run with --help
appended (argparse prints the help and exits before doing anything), in a throwaway folder.
"""

import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT  # noqa: E402

DOCS = os.path.join(ROOT, "docs")


def doc_files():
    files = [os.path.join(ROOT, "README.md")]
    files += sorted(os.path.join(DOCS, f) for f in os.listdir(DOCS) if f.endswith(".md"))
    return files


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def shell_commands(text):
    """The python3 command lines inside ```bash blocks, comments stripped, continuations joined."""
    out = []
    for block in re.findall(r"```(?:bash|sh|shell)\n(.*?)```", text, re.S):
        block = re.sub(r"\\\n\s*", " ", block)
        for line in block.splitlines():
            try:
                argv = shlex.split(line, comments=True)
            except ValueError:
                continue
            if len(argv) >= 2 and argv[0] == "python3" and argv[1].endswith(".py"):
                out.append(argv[1:])
    return out


def flags(argv):
    return sorted({a.split("=", 1)[0] for a in argv if re.match(r"^--[a-z][\w-]*(=.*)?$", a)})


class Commands(unittest.TestCase):
    def test_every_documented_command_parses(self):
        seen = set()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        work = tempfile.mkdtemp(prefix="bt-docs-")
        checked = 0
        for path in doc_files():
            rel = os.path.relpath(path, ROOT)
            for argv in shell_commands(read(path)):
                key = tuple(argv)
                if key in seen:
                    continue
                seen.add(key)
                script = os.path.join(ROOT, argv[0])
                self.assertTrue(os.path.isfile(script), "%s: no such script %s" % (rel, argv[0]))
                run = subprocess.run([sys.executable, script] + argv[1:] + ["--help"], cwd=work, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     timeout=60)
                cmd = " ".join(argv)
                self.assertEqual(run.returncode, 0, "%s: `python3 %s` does not parse:\n%s"
                                 % (rel, cmd, run.stderr.decode("utf-8", "replace")[-600:]))
                helptext = run.stdout.decode("utf-8", "replace")
                for f in flags(argv[1:]):
                    self.assertIn(f, helptext, "%s: `python3 %s`: %s is not an option" % (rel, cmd, f))
                checked += 1
        self.assertGreater(checked, 40)


class Links(unittest.TestCase):
    def test_relative_links_resolve(self):
        files = doc_files() + [os.path.join(ROOT, d, "README.md") for d in ("agents", "mcp")]
        for path in files:
            base = os.path.dirname(path)
            for target in re.findall(r"\]\(([^)\s]+)\)", read(path)):
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                target = target.split("#", 1)[0]
                self.assertTrue(os.path.exists(os.path.normpath(os.path.join(base, target))),
                                "%s links to a missing %s" % (os.path.relpath(path, ROOT), target))

    def test_docs_index_lists_every_page(self):
        index = read(os.path.join(DOCS, "README.md"))
        for f in os.listdir(DOCS):
            if f.endswith(".md") and f != "README.md":
                self.assertIn("(%s)" % f, index, "docs/README.md does not list %s" % f)

    def test_readme_lists_every_skill(self):
        readme = read(os.path.join(ROOT, "README.md"))
        skills = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertGreaterEqual(len(skills), 15)
        for s in skills:
            self.assertIn("| `%s` |" % s, readme, "README's skills table misses %s" % s)


if __name__ == "__main__":
    unittest.main()
