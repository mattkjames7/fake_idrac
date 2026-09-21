import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from fake_idrac import IPMIUDPHandler

IPMITOOL = shutil.which("ipmitool")


def test_ipmi_response_uses_response_netfn():
    request = bytes.fromhex("20 18 c8 81 20 38 8e 04 b6")
    handler = object.__new__(IPMIUDPHandler)
    response = handler.ipmi_response(request, bytes((0,)))

    assert response[1] >> 2 == (request[1] >> 2) + 1
    assert response[1] & 0x03 == request[1] & 0x03


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def fake_idrac():
    http_port, ipmi_port = free_port(), free_port()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "fake_idrac.py"), "--port", str(http_port),
         "--ipmi-port", str(ipmi_port), "--fans", "6", "--pwm", "30"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{http_port}/state"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(url, timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.02)
        else:
            output = process.stdout.read() if process.poll() is not None else ""
            pytest.fail(f"fake iDRAC did not start: {output}")
        yield http_port, ipmi_port
    finally:
        process.terminate()
        process.wait(timeout=2)


def ipmi(ipmi_port, *args, interface="lan"):
    return subprocess.run(
        [IPMITOOL, "-I", interface, "-H", "127.0.0.1", "-p", str(ipmi_port),
         "-U", "user", "-P", "pass", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_sets_all_six_fans(fake_idrac):
    http_port, ipmi_port = fake_idrac
    ipmi(ipmi_port, "raw", "0x30", "0x30", "0x02", "0xff", "0x46")

    state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/state"))
    assert state["manual"] is True
    assert len(state["fans"]) == 6
    assert {fan["pwm"] for fan in state["fans"]} == {70}


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_lanplus_sets_all_six_fans(fake_idrac):
    """Exercise a real IPMI v2.0/RMCP+ authenticated session end to end."""
    http_port, ipmi_port = fake_idrac
    ipmi(ipmi_port, "raw", "0x30", "0x30", "0x02", "0xff", "0x46",
         interface="lanplus")

    state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/state"))
    assert state["manual"] is True
    assert {fan["pwm"] for fan in state["fans"]} == {70}


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_switches_manual_control(fake_idrac):
    http_port, ipmi_port = fake_idrac
    ipmi(ipmi_port, "raw", "0x30", "0x30", "0x01", "0x00")
    assert json.load(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/state"))["manual"] is True

    ipmi(ipmi_port, "raw", "0x30", "0x30", "0x01", "0x01")
    assert json.load(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/state"))["manual"] is False


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_basic_queries_respond(fake_idrac):
    _, ipmi_port = fake_idrac
    assert "System Power" in ipmi(ipmi_port, "chassis", "status").stdout
    result = ipmi(ipmi_port, "sel", "elist")
    assert "SEL has no entries" in result.stderr


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_sdr_lists_six_fans(fake_idrac):
    _, ipmi_port = fake_idrac
    result = ipmi(ipmi_port, "sdr", "type", "Fan")
    assert result.stdout.count("Fan") == 6


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_sensor_list_names_six_fans(fake_idrac):
    _, ipmi_port = fake_idrac
    result = ipmi(ipmi_port, "sensor", "list")
    assert result.stdout.count("Fan") == 6


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_sensor_get_reports_fan(fake_idrac):
    _, ipmi_port = fake_idrac
    result = ipmi(ipmi_port, "sensor", "get", "Fan1")
    assert "Sensor Reading" in result.stdout
    assert "Status                : ok" in result.stdout


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
@pytest.mark.parametrize("command", [
    ("raw", "0x30", "0xce", "0x01", "0x16", "0x05", "0x00", "0x00", "0x00"),
    ("raw", "0x30", "0xce", "0x00", "0x16", "0x05", "0x00", "0x00", "0x00", "0x05", "0x00", "0x01", "0x00", "0x00"),
    ("raw", "0x30", "0xce", "0x00", "0x16", "0x05", "0x00", "0x00", "0x00", "0x05", "0x00", "0x00", "0x00", "0x00"),
    ("raw", "0x30", "0xce", "0x00", "0x09", "0x07", "0x00", "0x00", "0x00", "0x07", "0x00", "0x02", "0x02", "0x02", "0x00", "0x00"),
    ("raw", "0x30", "0xce", "0x00", "0x09", "0x07", "0x00", "0x00", "0x00", "0x07", "0x00", "0x02", "0x02", "0x02", "0x01", "0x00"),
])
def test_documented_oem_raw_commands_respond(fake_idrac, command):
    _, ipmi_port = fake_idrac
    ipmi(ipmi_port, *command)


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_documented_oem_commands_update_state(fake_idrac):
    http_port, ipmi_port = fake_idrac
    ipmi(ipmi_port, "raw", "0x30", "0xce", "0x00", "0x16", "0x05", "0x00", "0x00", "0x00",
         "0x05", "0x00", "0x01", "0x00", "0x00")
    state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/state"))
    assert state["pci_fan_response"] is False

    ipmi(ipmi_port, "raw", "0x30", "0xce", "0x00", "0x09", "0x07", "0x00", "0x00", "0x00",
         "0x07", "0x00", "0x02", "0x02", "0x02", "0x01", "0x00")
    state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/state"))
    assert state["fan_offset"] == "high"


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
@pytest.mark.parametrize("command", [
    ("sdr", "type", "Temperature"),
])
@pytest.mark.xfail(reason="temperature records are not implemented")
def test_documented_sensor_commands_report_fan_data(fake_idrac, command):
    _, ipmi_port = fake_idrac
    result = ipmi(ipmi_port, *command)
    assert "Fan" in (result.stdout + result.stderr)


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_sensor_reading_reports_rpm(fake_idrac):
    _, ipmi_port = fake_idrac
    result = ipmi(ipmi_port, "sensor", "reading", "Fan1")
    assert "Fan1" in result.stdout and "3000" in result.stdout


@pytest.mark.skipif(IPMITOOL is None, reason="ipmitool is not installed")
def test_ipmitool_sensor_threshold_write_updates_state(fake_idrac):
    http_port, ipmi_port = fake_idrac
    ipmi(ipmi_port, "sensor", "thresh", "Fan1", "lower", "0", "100", "200")
    state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/state"))
    assert state["fans"][0]["thresholds"][0] == 2
