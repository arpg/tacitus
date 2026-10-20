"""ea-doctor's check for an install still set up for the Outlook web token
path the core carried before v0.1.0 (arpg/tacitus#16): it fails, naming the
owner plugin, until an owner plugin supplies the Graph token and the old
token_refresh.py crontab line is gone. Inputs are injected; nothing reads
the host's crontab or lock."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ea_doctor_outlook_web", ROOT / "scripts" / "ea-doctor.py")
doctor = importlib.util.module_from_spec(_spec)
sys.modules["ea_doctor_outlook_web"] = doctor
_spec.loader.exec_module(doctor)

OLD_CRON = "*/30 * * * * cd /opt/ea-system && python3 preprocessor/token_refresh.py >> /tmp/t.log 2>&1\n"


@pytest.fixture(autouse=True)
def results():
    doctor.RESULTS.clear()
    yield doctor.RESULTS
    doctor.RESULTS.clear()


def _only():
    [r] = [r for r in doctor.RESULTS if r["name"].startswith("auth: ")]
    return r


def test_an_install_with_no_sign_of_the_old_path_hears_nothing():
    doctor.check_outlook_web_path({"GRAPH_AUTH_METHOD": "confidential"}, cron_text="", sources=[])
    doctor.check_outlook_web_path({}, cron_text="# token_refresh.py retired\n", sources=[])
    assert doctor.RESULTS == []


@pytest.mark.parametrize("env,cron", [({"GRAPH_AUTH_METHOD": "playwright"}, ""), ({}, OLD_CRON)])
def test_the_old_path_without_a_token_source_fails_and_names_the_plugin(env, cron):
    doctor.check_outlook_web_path(env, cron_text=cron, sources=[])
    r = _only()
    assert r["status"] == "FAIL" and "outlook-web-auth" in r["fix"]
    assert doctor.OUTLOOK_WEB_PLUGIN in r["fix"]


def test_the_plugin_installed_passes_once_the_old_cron_line_is_gone():
    doctor.check_outlook_web_path({"GRAPH_AUTH_METHOD": "playwright"}, cron_text=OLD_CRON,
                                  sources=["outlook-web-auth"])
    assert _only()["status"] == "FAIL" and "cron-lines" in _only()["fix"]
    doctor.RESULTS.clear()
    doctor.check_outlook_web_path({"GRAPH_AUTH_METHOD": "playwright"}, cron_text="",
                                  sources=["outlook-web-auth"])
    assert _only()["status"] == "PASS"


def test_an_unknown_method_is_named():
    doctor.check_outlook_web_path({"GRAPH_AUTH_METHOD": "magic"}, cron_text="", sources=[])
    assert _only()["status"] == "WARN" and "'magic' is not read" in _only()["detail"]
