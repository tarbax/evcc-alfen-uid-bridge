import logging
import os
import time
from typing import Callable

_HEALTH_FILE = "/tmp/bridge.healthy"

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

# Events dispatched to the orchestrator
CONNECTED = "connected"
DISCONNECTED = "disconnected"
EVCC_ONLINE = "evcc_online"
STARTUP_CHECK = "startup_check"


class MqttListener:
    """
    Subscribes to EVCC loadpoint MQTT topics and dispatches connect/disconnect
    events to the provided callback.

    Also subscribes to evcc/status — when EVCC (re)starts and publishes 'online',
    fires EVCC_ONLINE so the orchestrator can re-apply the vehicle assignment.

    Callback signature: cb(event: str) where event is CONNECTED, DISCONNECTED,
    or EVCC_ONLINE.
    """

    def __init__(self, host: str, port: int, username: str, password: str,
                 topic_prefix: str, loadpoint_id: int,
                 on_event: Callable[[str], None]):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._prefix = topic_prefix
        self._lp = loadpoint_id
        self._on_event = on_event

        self._connected_topic = f"{topic_prefix}/loadpoints/{loadpoint_id}/connected"
        self._status_topic = f"{topic_prefix}/status"

        self._last_connected: bool | None = None
        self._initial_retained_seen = False  # absorb the first retained message

        self._client = mqtt.Client(client_id="evcc-uid-bridge", clean_session=True)
        if username:
            self._client.username_pw_set(username, password)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("mqtt: connection to broker succeeded at %s:%d", self._host, self._port)
            client.subscribe(self._connected_topic)
            client.subscribe(self._status_topic)
            log.debug("mqtt: subscribed to %s, %s", self._connected_topic, self._status_topic)
            self._initial_retained_seen = False  # reset on reconnect
            try:
                open(_HEALTH_FILE, "w").close()
            except OSError:
                pass
        else:
            log.error("mqtt: connect failed rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        try:
            os.remove(_HEALTH_FILE)
        except OSError:
            pass
        if rc != 0:
            log.warning("mqtt: unexpected disconnect rc=%d — will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        topic = msg.topic
        log.debug("mqtt: %s = %s (retained=%s)", topic, payload, msg.retain)

        if topic == self._status_topic:
            if payload.lower() == "online" and not msg.retain:
                log.info("mqtt: EVCC came online")
                self._fire(EVCC_ONLINE)
            return

        if topic != self._connected_topic:
            return

        now_connected = payload.lower() == "true"

        # On (re)connect, absorb the retained message — don't fire CONNECTED/DISCONNECTED
        # since the RFID tap is likely outside the normal 300s window. Instead fire
        # STARTUP_CHECK when connected=true so the orchestrator can check EVCC state
        # and re-identify if needed using an extended lookback window.
        if not self._initial_retained_seen:
            self._initial_retained_seen = True
            self._last_connected = now_connected
            if now_connected:
                log.info("mqtt: startup: car is connected — triggering startup check")
                self._fire(STARTUP_CHECK)
            else:
                log.debug("mqtt: startup: no car connected")
            return

        if now_connected == self._last_connected:
            return  # no transition

        self._last_connected = now_connected
        event = CONNECTED if now_connected else DISCONNECTED
        log.info("mqtt: loadpoint %d → %s", self._lp, event)
        self._fire(event)

    def _fire(self, event: str):
        try:
            self._on_event(event)
        except Exception as exc:
            log.exception("mqtt: event handler raised: %s", exc)

    def start(self):
        """Connect and start the background network loop."""
        log.info("mqtt: connecting to %s:%d", self._host, self._port)
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()
        log.info("mqtt: disconnected")
