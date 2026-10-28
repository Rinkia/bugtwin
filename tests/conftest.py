import subprocess
from pathlib import Path

import pytest

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
       "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]


def git(root: Path, *args: str) -> str:
    return subprocess.run(GIT + list(args), cwd=root, check=True, capture_output=True,
                          text=True).stdout


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def make_repo(tmp_path):
    """make_repo(name, files) -> a committed git repo under tmp_path."""
    def make(name: str, files: dict[str, str]) -> Path:
        root = tmp_path / name
        root.mkdir()
        git(root, "init", "-q")
        write(root, files)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        return root
    return make


@pytest.fixture(autouse=True)
def bugtwin_home(tmp_path, monkeypatch):
    home = tmp_path / "_bugtwin_home"
    monkeypatch.setenv("BUGTWIN_HOME", str(home))
    monkeypatch.delenv("BUGTWIN_ALSO", raising=False)
    return home
