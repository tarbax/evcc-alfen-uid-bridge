import logging
import time

import requests

log = logging.getLogger(__name__)

_RETRY_DELAYS = [1, 3, 5]


class EvccClient:
    def __init__(self, base_url: str, loadpoint_id: int, dry_run: bool = False):
        self._base = base_url.rstrip("/")
        self._lp = loadpoint_id
        self._dry_run = dry_run

    def _post(self, path: str) -> bool:
        url = f"{self._base}{path}"
        if self._dry_run:
            log.info("evcc [dry-run]: POST %s", url)
            return True
        for delay in [0] + _RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                resp = requests.post(url, timeout=10)
                resp.raise_for_status()
                log.debug("evcc: POST %s → %d", url, resp.status_code)
                return True
            except Exception as exc:
                log.warning("evcc: POST %s failed: %s (retrying)", url, exc)
        log.error("evcc: giving up on POST %s", url)
        return False

    def _delete(self, path: str) -> bool:
        url = f"{self._base}{path}"
        if self._dry_run:
            log.info("evcc [dry-run]: DELETE %s", url)
            return True
        for delay in [0] + _RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                resp = requests.delete(url, timeout=10)
                resp.raise_for_status()
                log.debug("evcc: DELETE %s → %d", url, resp.status_code)
                return True
            except Exception as exc:
                log.warning("evcc: DELETE %s failed: %s (retrying)", url, exc)
        log.error("evcc: giving up on DELETE %s", url)
        return False

    def set_vehicle(self, vehicle_name: str) -> bool:
        path = f"/api/loadpoints/{self._lp}/vehicle/{vehicle_name}"
        ok = self._post(path)
        if ok:
            log.info("evcc: set loadpoint %d vehicle → %s", self._lp, vehicle_name)
        return ok

    def clear_vehicle(self) -> bool:
        path = f"/api/loadpoints/{self._lp}/vehicle"
        ok = self._delete(path)
        if ok:
            log.info("evcc: cleared loadpoint %d vehicle (back to auto-detection)", self._lp)
        return ok

    def get_vehicle(self) -> str:
        """Return this loadpoint's currently selected vehicle name, or '' if none/error.

        Read from /api/state → loadpoints[lp-1].vehicleName. There is no
        GET /api/loadpoints/{id}/vehicle endpoint (it 404s, e.g. on evcc
        0.310.x), so the loadpoint list in the global state is the source of
        truth. Loadpoint ids are 1-based in the API but the state array is
        0-indexed, hence lp-1.
        """
        url = f"{self._base}/api/state"
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            state = data.get("result", data)  # tolerate a 'result' wrapper if added later
            loadpoints = state.get("loadpoints", [])
            idx = self._lp - 1
            if 0 <= idx < len(loadpoints):
                return loadpoints[idx].get("vehicleName") or ""
            log.warning("evcc: loadpoint index %d out of range (%d loadpoints)", idx, len(loadpoints))
            return ""
        except Exception as exc:
            log.warning("evcc: get_vehicle failed: %s", exc)
            return ""

    def check_connection(self) -> bool:
        """Check EVCC's state API without changing any loadpoint state."""
        try:
            resp = requests.get(f"{self._base}/api/state", timeout=3)
            resp.raise_for_status()
            resp.json()
            return True
        except Exception as exc:
            log.debug("evcc: connection check failed: %s", exc)
            return False
