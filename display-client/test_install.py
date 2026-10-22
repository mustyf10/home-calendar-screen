"""Run installer branches only with rewritten sandbox paths and recording commands."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


CLIENT = Path(__file__).parent


@pytest.fixture
def installer(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("frame.py", "stamp.py", "frame.env.example", "home-calendar-frame.service", "home-calendar-frame.timer"):
        shutil.copyfile(CLIENT / name, source / name)
    shutil.copytree(CLIENT / "adapters", source / "adapters")
    root = tmp_path / "root"
    units = root / "etc/systemd/system"
    units.mkdir(parents=True)
    script = (CLIENT / "install.sh").read_text()
    for path in ("/etc/systemd/system", "/opt/home-calendar-frame", "/etc/home-calendar-frame", "/var/lib/home-calendar-frame"):
        script = script.replace(path, str(root) + path)
    # The real installer still requires sudo. This harness never gains privileges.
    script = script.replace("$EUID", "0")
    (source / "install.sh").write_text(script)
    commands = tmp_path / "commands"
    commands.mkdir()
    recorder = commands / "record"
    recorder.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["INSTALL_LOG"], "a") as out:
    out.write(json.dumps([name, *args]) + "\\n")
if name == "curl":
    pathlib.Path(args[args.index("-o") + 1]).write_text("vendor fixture")
if name == "install":
    if "-d" in args:
        pathlib.Path(args[-1]).mkdir(parents=True, exist_ok=True)
    else:
        for source in args[2:-1]:
            dest = pathlib.Path(args[-1])
            if dest.is_dir():
                dest = dest / pathlib.Path(source).name
            shutil.copyfile(source, dest)
''')
    recorder.chmod(0o755)
    for command in ("apt-get", "raspi-config", "usermod", "systemctl", "curl", "timedatectl", "install"):
        (commands / command).symlink_to(recorder)
    log = tmp_path / "calls.jsonl"
    environment = dict(os.environ, PATH=f"{commands}:{os.environ['PATH']}", SUDO_USER="testoperator", INSTALL_LOG=str(log))
    environment.pop("TIMEZONE", None)

    def run(*args):
        log.write_text("")
        result = subprocess.run(["bash", str(source / "install.sh"), *args], env=environment, capture_output=True, text=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    return root, run


@pytest.mark.parametrize("generic", [False, True])
def test_installer_modes_and_preserved_environment(installer, generic):
    root, run = installer
    arguments = ("--generic",) if generic else ()
    result, calls = run(*arguments)
    assert result.returncode == 0, result.stderr
    opt = root / "opt/home-calendar-frame"
    for name in ("frame.py", "stamp.py", "adapters/__init__.py", "adapters/waveshare7in5_v2.py"):
        assert (opt / name).read_bytes() == (CLIENT / name).read_bytes()
    assert not (opt / "stamp.py").is_symlink()
    units = root / "etc/systemd/system"
    assert "User=testoperator" in (units / "home-calendar-frame.service").read_text()
    assert (units / "home-calendar-frame.timer").is_file()
    env_file = root / "etc/home-calendar-frame/frame.env"
    assert env_file.read_bytes() == (CLIENT / "frame.env.example").read_bytes()
    env_file.write_text("FRAME_URL=http://keep-existing/\nFRAME_DRIVER=custom:create\n")
    result, second_calls = run(*arguments)
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "FRAME_URL=http://keep-existing/\nFRAME_DRIVER=custom:create\n"
    assert not any(call[0] == "install" and str(env_file) == call[-1] for call in second_calls)
    if generic:
        assert ["apt-get", "install", "-y", "python3-pil", "fonts-dejavu-core"] in calls
        assert not any(call[0] in ("raspi-config", "usermod", "curl") for call in calls)
        assert ["systemctl", "enable", "home-calendar-frame.timer"] in calls
        assert not any(call[0] == "systemctl" and ("--now" in call or "start" in call) for call in calls)
        assert not (opt / "waveshare_epd").exists()
        assert "FRAME_DRIVER" in result.stdout and "start home-calendar-frame.timer" in result.stdout
    else:
        assert ["raspi-config", "nonint", "do_spi", "0"] in calls
        assert ["usermod", "-aG", "spi,gpio", "testoperator"] in calls
        downloads = [call for call in calls if call[0] == "curl"]
        assert len(downloads) == 3
        assert all("a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc" in call[2] for call in downloads)
        assert ["systemctl", "enable", "--now", "home-calendar-frame.timer"] in calls


@pytest.mark.parametrize("arguments", [("--other",), ("--generic", "extra"), ("",)])
def test_installer_rejects_unknown_arguments_before_commands(installer, arguments):
    _, run = installer
    result, calls = run(*arguments)
    assert result.returncode == 1
    assert "Usage:" in result.stderr
    assert calls == []
