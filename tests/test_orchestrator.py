"""
Orchestrator integration tests with stubbed Alfen and EVCC clients.
Exercises connect/disconnect transitions and the dry-run path.
"""
import queue
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from bridge.config import Config, normalise_uid
from bridge.orchestrator import Orchestrator


def _make_config(**overrides):
    cfg = Config()
    cfg.alfen_host = "10.0.40.66"
    cfg.alfen_username = "admin"
    cfg.alfen_password = "test"
    cfg.alfen_socket = 1
    cfg.alfen_tls_verify = False
    cfg.tag_wait_timeout = 6
    cfg.tag_poll_interval = 1
    cfg.login_rate_max = 5
    cfg.login_rate_window = 60
    cfg.evcc_base_url = "http://127.0.0.1:7070"
    cfg.evcc_loadpoint_id = 1
    cfg.release_on_disconnect = True
    cfg.on_unknown_tag = "auto"
    cfg.default_vehicle = ""
    cfg.mqtt_host = "127.0.0.1"
    cfg.mqtt_port = 1883
    cfg.mqtt_username = ""
    cfg.mqtt_password = ""
    cfg.mqtt_topic_prefix = "evcc"
    cfg.uid_vehicle_map = {normalise_uid("04A1B2C3"): "bmw320e"}
    cfg.dry_run = False
    cfg.log_level = "DEBUG"
    cfg.log_uid_plaintext = True
    cfg.backoffice_check_enabled = False  # not under test here; set by Config.from_env() only
    cfg.notify_url = ""
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _run_event(cfg, event, alfen_uid=None):
    """
    Run the orchestrator event handler directly (bypassing MQTT).
    Returns the last call made on the EVCC client.
    """
    evcc_calls = []

    with patch("bridge.orchestrator.AlfenClient") as MockAlfen, \
         patch("bridge.orchestrator.EvccClient") as MockEvcc, \
         patch("bridge.orchestrator.MqttListener"):

        mock_alfen_inst = MagicMock()
        mock_alfen_inst.login.return_value = True
        mock_alfen_inst.get_latest_tag.return_value = alfen_uid
        MockAlfen.return_value = mock_alfen_inst

        mock_evcc_inst = MagicMock()
        mock_evcc_inst.set_vehicle.return_value = True
        mock_evcc_inst.clear_vehicle.return_value = True
        mock_evcc_inst.get_vehicle.return_value = ""
        MockEvcc.return_value = mock_evcc_inst

        orch = Orchestrator(cfg)
        orch._evcc = mock_evcc_inst

        if event == "connected":
            orch._handle_connect(datetime.now(tz=timezone.utc))
        elif event == "disconnected":
            orch._handle_disconnect()

        return mock_evcc_inst


def test_known_uid_sets_vehicle():
    cfg = _make_config()
    evcc = _run_event(cfg, "connected", alfen_uid="04A1B2C3")
    evcc.set_vehicle.assert_called_once_with("bmw320e")


def test_unknown_uid_auto_mode_no_action():
    cfg = _make_config(on_unknown_tag="auto")
    evcc = _run_event(cfg, "connected", alfen_uid="DEADBEEF")
    evcc.set_vehicle.assert_not_called()


def test_unknown_uid_default_mode_sets_default():
    cfg = _make_config(on_unknown_tag="default", default_vehicle="bmw320e")
    evcc = _run_event(cfg, "connected", alfen_uid="DEADBEEF")
    evcc.set_vehicle.assert_called_once_with("bmw320e")


def test_no_tag_found_auto_mode_no_action():
    cfg = _make_config(tag_wait_timeout=2, tag_poll_interval=1)
    evcc = _run_event(cfg, "connected", alfen_uid=None)
    evcc.set_vehicle.assert_not_called()


def test_disconnect_releases_vehicle():
    cfg = _make_config(release_on_disconnect=True)
    evcc = _run_event(cfg, "disconnected")
    evcc.clear_vehicle.assert_called_once()


def test_disconnect_no_release_when_disabled():
    cfg = _make_config(release_on_disconnect=False)
    evcc = _run_event(cfg, "disconnected")
    evcc.clear_vehicle.assert_not_called()


def test_idempotency_no_duplicate_set():
    cfg = _make_config()
    with patch("bridge.orchestrator.AlfenClient") as MockAlfen, \
         patch("bridge.orchestrator.EvccClient") as MockEvcc, \
         patch("bridge.orchestrator.MqttListener"):

        mock_alfen = MagicMock()
        mock_alfen.login.return_value = True
        mock_alfen.get_latest_tag.return_value = "04A1B2C3"
        MockAlfen.return_value = mock_alfen

        mock_evcc = MagicMock()
        mock_evcc.set_vehicle.return_value = True
        MockEvcc.return_value = mock_evcc

        orch = Orchestrator(cfg)
        orch._evcc = mock_evcc

        ts = datetime.now(tz=timezone.utc)
        orch._handle_connect(ts)
        orch._handle_connect(ts)  # second connect with same vehicle

    assert mock_evcc.set_vehicle.call_count == 1  # only once


def test_dry_run_does_not_call_evcc():
    cfg = _make_config(dry_run=True)
    with patch("bridge.orchestrator.AlfenClient") as MockAlfen, \
         patch("bridge.orchestrator.MqttListener"):

        mock_alfen = MagicMock()
        mock_alfen.login.return_value = True
        mock_alfen.get_latest_tag.return_value = "04A1B2C3"
        MockAlfen.return_value = mock_alfen

        # Use a real EvccClient in dry_run mode — it logs but doesn't HTTP
        from bridge.evcc_client import EvccClient
        real_evcc = EvccClient("http://127.0.0.1:7070", 1, dry_run=True)

        with patch("requests.post") as mock_post, patch("requests.delete") as mock_del:
            orch = Orchestrator(cfg)
            orch._evcc = real_evcc
            orch._handle_connect(datetime.now(tz=timezone.utc))
            mock_post.assert_not_called()
            mock_del.assert_not_called()


# ---- EVCC restart recovery ---------------------------------------------------

def _make_orch(cfg, alfen_uid=None, evcc_vehicle=""):
    """
    Build an Orchestrator with a stubbed Alfen factory, EVCC client, and MQTT
    listener.  The AlfenClient patch is intentionally NOT kept alive for the
    duration of the test — instead we replace orch._make_alfen() with a lambda
    that always returns the same mock.  This avoids leaking real HTTPS calls when
    the patch context manager exits before test methods run.
    """
    mock_alfen = MagicMock()
    mock_alfen.login.return_value = True
    mock_alfen.get_latest_tag.return_value = alfen_uid

    mock_evcc = MagicMock()
    mock_evcc.set_vehicle.return_value = True
    mock_evcc.clear_vehicle.return_value = True
    mock_evcc.get_vehicle.return_value = evcc_vehicle

    with patch("bridge.orchestrator.EvccClient", return_value=mock_evcc), \
         patch("bridge.orchestrator.MqttListener"):
        orch = Orchestrator(cfg)

    # Replace the factory so every _make_alfen() call returns our mock instance.
    orch._make_alfen = lambda: mock_alfen
    orch._evcc = mock_evcc
    orch._mock_alfen = mock_alfen
    return orch


def test_evcc_online_sets_flag_and_clears_vehicle():
    cfg = _make_config()
    orch = _make_orch(cfg)
    orch._current_vehicle = "bmw320e"

    orch._handle_evcc_online()

    assert orch._evcc_just_restarted is True
    assert orch._current_vehicle is None


def test_evcc_restart_connect_uses_extended_lookback():
    """After EVCC restart, _handle_connect must scan with max_pages=700."""
    cfg = _make_config()
    orch = _make_orch(cfg, alfen_uid="04A1B2C3")
    orch._evcc_just_restarted = True

    orch._handle_connect(datetime.now(tz=timezone.utc))

    _, kwargs = orch._mock_alfen.get_latest_tag.call_args
    assert kwargs.get("max_pages") == 700
    orch._evcc.set_vehicle.assert_called_once_with("bmw320e")


def test_evcc_restart_connect_clears_flag():
    """The _evcc_just_restarted flag must be consumed by the first connect."""
    cfg = _make_config()
    orch = _make_orch(cfg, alfen_uid="04A1B2C3")
    orch._evcc_just_restarted = True

    orch._handle_connect(datetime.now(tz=timezone.utc))

    assert orch._evcc_just_restarted is False


def test_normal_connect_does_not_use_max_pages_100():
    """A normal connect (no restart) must use the default max_pages, not 100."""
    cfg = _make_config()
    orch = _make_orch(cfg, alfen_uid="04A1B2C3")
    # _evcc_just_restarted is False by default

    orch._handle_connect(datetime.now(tz=timezone.utc))

    call_kwargs = orch._mock_alfen.get_latest_tag.call_args
    # max_pages should not be explicitly set (uses the alfen_client default of 20)
    assert call_kwargs.kwargs.get("max_pages", 20) == 20


# ---- Startup check -----------------------------------------------------------

def test_startup_check_known_vehicle_no_alfen_call():
    """If EVCC already has a known vehicle, startup check must not touch Alfen."""
    cfg = _make_config()
    orch = _make_orch(cfg, evcc_vehicle="bmw320e")

    orch._handle_startup_check()

    orch._mock_alfen.login.assert_not_called()
    orch._evcc.set_vehicle.assert_not_called()
    assert orch._current_vehicle == "bmw320e"


def test_startup_check_unset_vehicle_scans_log():
    """If EVCC has no vehicle, startup check must scan Alfen and set vehicle."""
    cfg = _make_config()
    orch = _make_orch(cfg, alfen_uid="04A1B2C3", evcc_vehicle="")

    orch._handle_startup_check()

    orch._evcc.set_vehicle.assert_called_once_with("bmw320e")
    _, kwargs = orch._mock_alfen.get_latest_tag.call_args
    assert kwargs.get("max_pages") == 700


def test_startup_check_unknown_vehicle_name_triggers_scan():
    """'unknown' in EVCC (not in uid_map values) must trigger re-identification."""
    cfg = _make_config()
    orch = _make_orch(cfg, alfen_uid="04A1B2C3", evcc_vehicle="unknown")

    orch._handle_startup_check()

    orch._evcc.set_vehicle.assert_called_once_with("bmw320e")


def test_startup_check_log_empty_sets_default_vehicle():
    """If log scan finds no tag and ON_UNKNOWN_TAG=default, must set default vehicle."""
    cfg = _make_config(on_unknown_tag="default", default_vehicle="fallback")
    orch = _make_orch(cfg, alfen_uid=None, evcc_vehicle="")

    orch._handle_startup_check()

    orch._evcc.set_vehicle.assert_called_once_with("fallback")


def test_startup_check_log_empty_auto_mode_no_action():
    """If log scan finds no tag and ON_UNKNOWN_TAG=auto, must leave EVCC alone."""
    cfg = _make_config(on_unknown_tag="auto")
    orch = _make_orch(cfg, alfen_uid=None, evcc_vehicle="")

    orch._handle_startup_check()

    orch._evcc.set_vehicle.assert_not_called()


def test_hard_crash_recovery_sets_vehicle():
    """
    Simulate EVCC hard crash: no connected=false published, so _current_vehicle
    is stale. After evcc_online + connect, vehicle must still be (re-)applied.
    """
    cfg = _make_config()
    orch = _make_orch(cfg, alfen_uid="04A1B2C3")
    orch._current_vehicle = "bmw320e"  # stale — EVCC restarted without clearing

    orch._handle_evcc_online()          # clears _current_vehicle, sets flag
    orch._handle_connect(datetime.now(tz=timezone.utc))

    orch._evcc.set_vehicle.assert_called_once_with("bmw320e")
