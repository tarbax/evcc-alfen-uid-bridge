"""
Orchestrator: ties together MQTT events, Alfen tag acquisition, and EVCC actions.

On CONNECTED:
  1. Record connect timestamp.
  2. Poll Alfen log for a tag newer than that timestamp for up to TAG_WAIT_TIMEOUT_S.
  3. Map the UID to an EVCC vehicle name.
  4. Set the vehicle in EVCC (unless already selected or dry-run).

On DISCONNECTED:
  1. Optionally release the vehicle selection in EVCC.

Alfen access is serialised via a lock (single-session requirement).
"""

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .alfen_client import AlfenClient, RateLimiter, uid_hash
from .config import Config, normalise_uid
from .evcc_client import EvccClient
from .mqtt_listener import CONNECTED, DISCONNECTED, EVCC_ONLINE, STARTUP_CHECK, MqttListener

log = logging.getLogger(__name__)

_SENTINEL = object()
_STARTUP_LOOKBACK_S = 259200  # scan up to 72 h back when recovering a missed session


class Orchestrator:
    def __init__(self, config: Config):
        self._cfg = config
        self._event_queue: queue.Queue = queue.Queue()
        self._alfen_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._current_vehicle: Optional[str] = None
        self._car_connected: bool = False
        self._evcc_just_restarted: bool = False

        self._rate_limiter = RateLimiter(config.login_rate_max, config.login_rate_window)

        self._evcc = EvccClient(
            config.evcc_base_url,
            config.evcc_loadpoint_id,
            config.dry_run,
        )

        self._listener = MqttListener(
            host=config.mqtt_host,
            port=config.mqtt_port,
            username=config.mqtt_username,
            password=config.mqtt_password,
            topic_prefix=config.mqtt_topic_prefix,
            loadpoint_id=config.evcc_loadpoint_id,
            on_event=self._dispatch,
        )

    def _dispatch(self, event: str):
        """Called from the MQTT callback thread — just enqueue."""
        self._event_queue.put((event, datetime.now(tz=timezone.utc)))

    def _make_alfen(self) -> AlfenClient:
        cfg = self._cfg
        return AlfenClient(
            host=cfg.alfen_host,
            username=cfg.alfen_username,
            password=cfg.alfen_password,
            socket=cfg.alfen_socket,
            tls_verify=cfg.alfen_tls_verify,
            rate_limiter=self._rate_limiter,
            log_uid_plaintext=cfg.log_uid_plaintext,
        )

    def _apply_vehicle_for_uid(self, uid: str):
        """Map a UID to an EVCC vehicle name and set it, or apply unknown-tag behaviour."""
        normalised = normalise_uid(uid)
        uid_label = uid if self._cfg.log_uid_plaintext else uid_hash(normalised)
        log.info("orchestrator: tag acquired uid_hash=%s", uid_label)

        vehicle = self._cfg.uid_vehicle_map.get(normalised)
        if not vehicle:
            # State-line UIDs may be truncated by firmware (e.g. "5B9F" instead of
            # "5B9F4379"). Try prefix match: find map keys that start with the found UID.
            prefix_matches = {k: v for k, v in self._cfg.uid_vehicle_map.items()
                              if k.startswith(normalised)}
            if len(prefix_matches) == 1:
                vehicle = next(iter(prefix_matches.values()))
                log.info("orchestrator: UID %s matched by prefix to %s",
                         uid_label, next(iter(prefix_matches)))
            elif len(prefix_matches) > 1:
                log.warning("orchestrator: UID %s is an ambiguous prefix (%d matches), skipping",
                            uid_label, len(prefix_matches))

        if not vehicle:
            log.warning("orchestrator: UID %s not in map", uid_label)
            self._apply_unknown_tag()
            return

        if self._current_vehicle == vehicle:
            log.info("orchestrator: vehicle %s already selected, no action", vehicle)
            return

        if self._evcc.set_vehicle(vehicle):
            self._current_vehicle = vehicle

    def _identify_from_log(self, lookback_s: int) -> Optional[str]:
        """Single-shot historical Alfen log scan — no deadline, up to 700 pages."""
        since = datetime.now(tz=timezone.utc)
        with self._alfen_lock:
            alfen = self._make_alfen()
            if not alfen.login():
                log.warning("orchestrator: could not login to Alfen")
                return None
            try:
                self._check_and_notify_backoffice(alfen)
                return alfen.get_latest_tag(since=since, lookback_s=lookback_s, max_pages=700)
            finally:
                alfen.logout()

    def _handle_connect(self, connect_time: datetime):
        self._car_connected = True

        # EVCC just restarted: the tap happened in the past — historical scan, no deadline
        if self._evcc_just_restarted:
            self._evcc_just_restarted = False
            log.info("orchestrator: EVCC restart detected — extended 72h log scan")
            uid = self._identify_from_log(_STARTUP_LOOKBACK_S)
            if not uid:
                log.warning("orchestrator: extended log scan found no tag")
                self._apply_unknown_tag()
            else:
                self._apply_vehicle_for_uid(uid)
            return

        # Normal connect: poll with deadline — the tap may not be in the log yet
        uid: Optional[str] = None

        with self._alfen_lock:
            alfen = self._make_alfen()
            if not alfen.login():
                log.warning("orchestrator: could not login to Alfen, leaving EVCC on auto-detection")
                return

            try:
                self._check_and_notify_backoffice(alfen)

                deadline = time.monotonic() + self._cfg.tag_wait_timeout
                poll = self._cfg.tag_poll_interval

                while time.monotonic() < deadline:
                    uid = alfen.get_latest_tag(since=connect_time)
                    if uid:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sleep_s = min(poll, remaining)
                    log.debug("orchestrator: no tag yet, retrying in %.0fs", sleep_s)
                    time.sleep(sleep_s)
            finally:
                alfen.logout()

        if not uid:
            log.warning("orchestrator: no RFID tag found within %ds window", self._cfg.tag_wait_timeout)
            self._apply_unknown_tag()
            return

        self._apply_vehicle_for_uid(uid)

    def _handle_disconnect(self):
        self._car_connected = False
        if not self._cfg.release_on_disconnect:
            log.debug("orchestrator: release_on_disconnect=false, keeping selection")
            return
        if self._evcc.clear_vehicle():
            self._current_vehicle = None

    def _handle_evcc_online(self):
        self._current_vehicle = None

        if self._car_connected:
            # Car is already connected. On a clean EVCC shutdown it will have published
            # connected=false first, resetting _car_connected. If it didn't (hard crash or
            # fast restart), the retained connected=true MQTT message will be a no-op
            # (same state → ignored by the listener). Don't wait for a second MQTT event
            # that may never arrive — identify immediately.
            log.info("orchestrator: EVCC came online with car connected — extended 72h log scan")
            uid = self._identify_from_log(_STARTUP_LOOKBACK_S)
            if not uid:
                log.warning("orchestrator: extended log scan found no tag")
                self._apply_unknown_tag()
            else:
                self._apply_vehicle_for_uid(uid)
        else:
            # Car not connected yet; set flag so the next plug-in uses extended lookback.
            log.info("orchestrator: EVCC came online — next connect event will use extended lookback")
            self._evcc_just_restarted = True

    def _handle_startup_check(self):
        """
        Called once on startup when a car is already connected.
        If EVCC has a known vehicle assigned, trust it and record state.
        If EVCC shows unknown/guest/empty, do a single extended 72h log scan
        (no deadline — the tap happened in the past).
        """
        self._car_connected = True
        current = self._evcc.get_vehicle()
        known = set(self._cfg.uid_vehicle_map.values())

        if current and current in known:
            log.info("orchestrator: startup: car connected, vehicle=%s already set — no action", current)
            self._current_vehicle = current
            return

        log.info("orchestrator: startup: car connected but vehicle=%r — identifying via Alfen (72h lookback)", current)
        uid = self._identify_from_log(_STARTUP_LOOKBACK_S)
        if not uid:
            log.warning("orchestrator: startup: log scan found no tag")
            self._apply_unknown_tag()
            return
        self._apply_vehicle_for_uid(uid)

    def _check_and_notify_backoffice(self, alfen: AlfenClient):
        """Advisory back-office connectivity check on an already-authenticated session.

        Reads BOConnection from /api/info; fires a notification if not "online".
        Any error is caught, logged, and ignored — this must never affect RFID/vehicle flow.
        """
        if not self._cfg.backoffice_check_enabled:
            return
        try:
            bo_status = alfen.get_bo_connection()
            if bo_status is None:
                log.debug("orchestrator: BOConnection not available — skipping check")
                return
            log.debug("orchestrator: BOConnection = %r", bo_status)
            if bo_status != "online":
                log.warning("orchestrator: Alfen back office is OFFLINE (BOConnection=%r)", bo_status)
                self._notify_backoffice_offline()
        except Exception as exc:
            log.warning("orchestrator: back-office check failed (ignored): %s", exc)

    def _notify_backoffice_offline(self):
        """Fire-and-forget HTTP POST to NOTIFY_URL with a plain-text offline alert."""
        now_str = datetime.now().strftime("%H:%M:%S %d/%m/%Y")
        msg = (
            f"Alfen back office OFFLINE - charger not connected to OCPP back office "
            f"(seen at session start {now_str})."
        )
        url = self._cfg.notify_url

        def _post():
            try:
                resp = requests.post(
                    url,
                    json={"title": "EV charger", "msg": msg},
                    timeout=5,
                )
                log.info("orchestrator: back-office offline notification sent (HTTP %s)", resp.status_code)
            except Exception as exc:
                log.warning("orchestrator: notification send failed (ignored): %s", exc)

        threading.Thread(target=_post, daemon=True, name="backoffice-notify").start()

    def _apply_unknown_tag(self):
        if self._cfg.on_unknown_tag == "default" and self._cfg.default_vehicle:
            if self._current_vehicle != self._cfg.default_vehicle:
                if self._evcc.set_vehicle(self._cfg.default_vehicle):
                    self._current_vehicle = self._cfg.default_vehicle
        # else: leave on auto-detection

    def _event_loop(self):
        log.info("orchestrator: event processor started")
        while not self._shutdown.is_set():
            try:
                item = self._event_queue.get(timeout=1)
            except queue.Empty:
                continue

            if item is _SENTINEL:
                break

            event, ts = item
            log.debug("orchestrator: processing event=%s ts=%s", event, ts.isoformat())

            if event == CONNECTED:
                self._handle_connect(ts)
            elif event == DISCONNECTED:
                self._handle_disconnect()
            elif event == EVCC_ONLINE:
                self._handle_evcc_online()
            elif event == STARTUP_CHECK:
                self._handle_startup_check()
            else:
                log.warning("orchestrator: unknown event %s", event)

            self._event_queue.task_done()

        log.info("orchestrator: event processor stopped")

    def run(self):
        processor = threading.Thread(target=self._event_loop, daemon=True, name="event-processor")
        processor.start()

        self._listener.start()
        log.info("orchestrator: running — waiting for events")

        self._shutdown.wait()

        self._listener.stop()
        self._event_queue.put(_SENTINEL)
        processor.join(timeout=10)

    def shutdown(self):
        log.info("orchestrator: shutdown requested")
        self._shutdown.set()
