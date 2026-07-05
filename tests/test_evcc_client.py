"""
Unit tests for EvccClient.get_vehicle().

Regression cover for the endpoint fix: the vehicle must be read from
/api/state -> loadpoints[lp-1].vehicleName, not the non-existent
GET /api/loadpoints/{id}/vehicle (which 404s on evcc 0.310.x).
"""
from unittest.mock import MagicMock, patch

from bridge.evcc_client import EvccClient


def _resp(json_body, status=200):
    r = MagicMock()
    r.json.return_value = json_body
    r.raise_for_status.side_effect = None if status < 400 else Exception(f"HTTP {status}")
    return r


def _state(loadpoints, wrap_result=False):
    body = {"loadpoints": loadpoints}
    return {"result": body} if wrap_result else body


def test_get_vehicle_reads_correct_loadpoint_index():
    # lp id 2 (1-based) must read loadpoints[1] (0-based)
    client = EvccClient("http://x:7070", loadpoint_id=2)
    state = _state([{"vehicleName": "audiq6"}, {"vehicleName": "bmwx130e"}])
    with patch("bridge.evcc_client.requests.get", return_value=_resp(state)) as g:
        assert client.get_vehicle() == "bmwx130e"
    # confirms it hits /api/state, not the dead per-loadpoint endpoint
    assert g.call_args[0][0].endswith("/api/state")


def test_get_vehicle_empty_when_no_vehicle():
    client = EvccClient("http://x:7070", loadpoint_id=1)
    state = _state([{"vehicleName": ""}])
    with patch("bridge.evcc_client.requests.get", return_value=_resp(state)):
        assert client.get_vehicle() == ""


def test_get_vehicle_missing_field_returns_empty():
    client = EvccClient("http://x:7070", loadpoint_id=1)
    state = _state([{}])  # vehicleName absent -> ""
    with patch("bridge.evcc_client.requests.get", return_value=_resp(state)):
        assert client.get_vehicle() == ""


def test_get_vehicle_tolerates_result_wrapper():
    client = EvccClient("http://x:7070", loadpoint_id=1)
    state = _state([{"vehicleName": "audiq6"}], wrap_result=True)
    with patch("bridge.evcc_client.requests.get", return_value=_resp(state)):
        assert client.get_vehicle() == "audiq6"


def test_get_vehicle_out_of_range_returns_empty():
    client = EvccClient("http://x:7070", loadpoint_id=5)
    state = _state([{"vehicleName": "audiq6"}])
    with patch("bridge.evcc_client.requests.get", return_value=_resp(state)):
        assert client.get_vehicle() == ""


def test_get_vehicle_swallows_http_error():
    client = EvccClient("http://x:7070", loadpoint_id=1)
    with patch("bridge.evcc_client.requests.get", return_value=_resp({}, status=500)):
        assert client.get_vehicle() == ""


def test_get_vehicle_swallows_connection_error():
    client = EvccClient("http://x:7070", loadpoint_id=1)
    with patch("bridge.evcc_client.requests.get", side_effect=OSError("boom")):
        assert client.get_vehicle() == ""
