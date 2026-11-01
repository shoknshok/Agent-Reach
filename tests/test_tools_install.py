"""Download diagnostics and repeatable setup, without network access."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_reach import __version__
from agent_reach.runtime import tools
from agent_reach.runtime.result import TIMEOUT, ReachError


def _no_proxy(monkeypatch):
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tools, "extend_path", lambda: None)


def test_package_version_matches_runtime_version():
    # The package metadata and --version must describe the same release.
    import re

    project = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', project, re.MULTILINE).group(1) == __version__


def test_download_env_copies_system_proxy_for_both_install_stages(monkeypatch):
    _no_proxy(monkeypatch)
    monkeypatch.setattr(tools.urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:8899", "no": "localhost"})
    env = tools.install_env()
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:8899"
    assert env["NO_PROXY"] == "localhost"
    assert "HTTPS_PROXY" not in os.environ


@pytest.mark.parametrize("key", ["https_proxy", "HTTP_PROXY", "ALL_PROXY"])
def test_explicit_proxy_is_kept_without_consulting_system(monkeypatch, key):
    _no_proxy(monkeypatch)
    monkeypatch.setenv(key, "http://127.0.0.1:7788")
    monkeypatch.setattr(tools.urllib.request, "getproxies", lambda: pytest.fail("explicit proxy must win"))
    env = tools.install_env()
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:7788"


def test_pip_timeout_names_stage_and_redacts_proxy(monkeypatch):
    monkeypatch.setattr(tools, "_install_env", lambda: {"HTTPS_PROXY": "http://user:secret@127.0.0.1:7788"})

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(tools.subprocess, "run", timeout)
    with pytest.raises(ReachError) as err:
        tools._run(["python", "-m", "pip"], timeout=1, stage="cloakbrowser:pip_packages")
    assert err.value.code == TIMEOUT
    assert "cloakbrowser:pip_packages" in err.value.message
    assert "configured proxy" in err.value.hint
    assert "secret" not in str(err.value) + err.value.hint


def test_install_failure_scrubs_upstream_url_credentials(monkeypatch):
    monkeypatch.setattr(tools, "_install_env", lambda: {})
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="Could not connect to https://user:secret@example.com/download?token=abc"))
    with pytest.raises(ReachError) as err:
        tools._run(["pip", "install", "cloakbrowser"], stage="cloakbrowser:pip_packages")
    assert "secret" not in str(err.value)
    assert "token=abc" not in str(err.value)
    assert "direct network" in err.value.hint


def test_rerunning_pip_setup_preserves_partial_environment(monkeypatch, tmp_path):
    tool = tools.TOOLS["cloakbrowser"]
    monkeypatch.setattr(tools, "tool_dir", lambda name: tmp_path / name)
    venv = tmp_path / tool.name / "venv"
    py = venv / ("Scripts/python.exe" if tools._WIN else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text("already installed interpreter")
    marker = venv / "partially-installed-package"
    marker.write_text("keep")
    calls = []
    monkeypatch.setattr(tools, "_run", lambda argv, **kwargs: calls.append((argv, kwargs)) or "")
    tools._install_pip(tool)
    assert marker.read_text() == "keep"
    assert len(calls) == 1
    assert calls[0][0][:4] == [str(py), "-m", "pip", "install"]
    assert calls[0][1]["stage"] == "cloakbrowser:pip_packages"
    assert "cloakbrowser==0.6.3" in calls[0][0]


def test_opencli_uses_pristine_compatible_release_without_install_hooks(monkeypatch, tmp_path):
    from agent_reach.runtime import tools
    calls=[]
    monkeypatch.setattr(tools,"_npm_cmd",lambda:"npm")
    monkeypatch.setattr(tools,"_run",lambda argv,**kwargs:calls.append(argv) or "")
    tools._install_npm(tools.TOOLS["opencli"],global_prefix=tmp_path/"npm")
    assert "--ignore-scripts" in calls[0]
    assert calls[0][-1]=="@jackwener/opencli@1.8.7"
    assert not tools.allowed(tools.TOOLS["opencli"],"1.8.8")


def _tar_gz(path, files):
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return path


def test_uv_install_uses_the_pinned_release_and_verifies_its_checksum(monkeypatch, tmp_path):
    import hashlib

    _no_proxy(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tools, "tools_root", lambda: tmp_path / "tools")
    name = "uv-x86_64-unknown-linux-gnu.tar.gz"
    monkeypatch.setattr(tools, "_uv_archive_name", lambda: name)
    archive = _tar_gz(tmp_path / name, {"uv-x86_64-unknown-linux-gnu/uv": b"uv", "uv-x86_64-unknown-linux-gnu/uvx": b"uvx",
                                         "uv-x86_64-unknown-linux-gnu/../../evil": b"x"})
    monkeypatch.setitem(tools._UV_SHA256, name, hashlib.sha256(archive.read_bytes()).hexdigest())
    urls = []
    monkeypatch.setattr(tools, "_download", lambda url, dest, timeout=600: urls.append(url) or dest.write_bytes(archive.read_bytes()))
    monkeypatch.setattr(tools, "_WIN", False)

    tools._install_uv(tools.TOOLS["uv"])

    assert urls == [f"https://github.com/astral-sh/uv/releases/download/{tools.UV_VERSION}/{name}"]
    assert (tmp_path / "tools" / "uv" / "bin" / "uvx").read_bytes() == b"uvx"
    assert not (tmp_path / "evil").exists()


def test_uv_install_rejects_a_tampered_archive(monkeypatch, tmp_path):
    _no_proxy(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tools, "tools_root", lambda: tmp_path / "tools")
    name = "uv-x86_64-unknown-linux-gnu.tar.gz"
    monkeypatch.setattr(tools, "_uv_archive_name", lambda: name)
    monkeypatch.setattr(tools, "_download", lambda url, dest, timeout=600: dest.write_bytes(b"not the real uv"))

    with pytest.raises(ReachError, match="checksum mismatch"):
        tools._install_uv(tools.TOOLS["uv"])
    assert not (tmp_path / "tools" / "uv" / "bin").exists()


def test_uv_never_runs_a_downloaded_install_script():
    source = Path(tools.__file__).read_text()
    assert "install.sh" not in source and "install.ps1" not in source
