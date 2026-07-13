import json
import logging
import os
import re
import sys

log = logging.getLogger(__name__)

_REDACT = re.compile(r"password|secret|token", re.I)


def _env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        sys.exit(f"[config] required env var {key} is not set")
    return val


def _bool(val):
    return str(val).lower() in ("1", "true", "yes")


def _uid_map(raw):
    """Parse UID_VEHICLE_MAP from a JSON string or a file path."""
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        data = json.loads(raw)
    else:
        with open(raw) as f:
            data = json.load(f)
    return {normalise_uid(k): v for k, v in data.items()}


def normalise_uid(uid: str) -> str:
    """Strip non-alphanumeric chars and uppercase — canonical form for matching."""
    return re.sub(r"[^0-9a-fA-F]", "", uid).upper()


class Config:
    def __init__(self):
        pass

    @classmethod
    def from_env(cls):
        c = cls()
        c.alfen_host = _env("ALFEN_HOST", required=True)
        c.alfen_username = _env("ALFEN_USERNAME", "admin")
        c.alfen_password = _env("ALFEN_PASSWORD", required=True)
        c.alfen_socket = int(_env("ALFEN_SOCKET", "1"))
        c.alfen_tls_verify = _bool(_env("ALFEN_TLS_VERIFY", "false"))

        c.tag_wait_timeout = int(_env("TAG_WAIT_TIMEOUT_S", "15"))
        c.tag_poll_interval = int(_env("TAG_POLL_INTERVAL_S", "3"))
        c.login_rate_max = int(_env("LOGIN_RATE_MAX", "8"))
        c.login_rate_window = int(_env("LOGIN_RATE_WINDOW_S", "60"))
        # The Alfen is often busy negotiating the session at the exact moment a car
        # plugs in, so the first login can time out. Retry a few times before giving
        # up and leaving EVCC on auto-detection (which yields an "unknown" vehicle).
        c.login_retries = int(_env("LOGIN_RETRIES", "4"))
        c.login_retry_backoff = float(_env("LOGIN_RETRY_BACKOFF_S", "5"))

        c.evcc_base_url = _env("EVCC_BASE_URL", required=True).rstrip("/")
        c.evcc_loadpoint_id = int(_env("EVCC_LOADPOINT_ID", required=True))

        c.release_on_disconnect = _bool(_env("RELEASE_ON_DISCONNECT", "true"))
        c.on_unknown_tag = _env("ON_UNKNOWN_TAG", "auto")
        c.default_vehicle = _env("DEFAULT_VEHICLE", "")

        c.mqtt_host = _env("MQTT_HOST", required=True)
        c.mqtt_port = int(_env("MQTT_PORT", "1883"))
        c.mqtt_username = _env("MQTT_USERNAME", "")
        c.mqtt_password = _env("MQTT_PASSWORD", "")
        c.mqtt_topic_prefix = _env("MQTT_TOPIC_PREFIX", "evcc")

        raw_map = _env("UID_VEHICLE_MAP", required=True)
        c.uid_vehicle_map = _uid_map(raw_map)

        c.dry_run = _bool(_env("DRY_RUN", "false"))
        c.log_level = _env("LOG_LEVEL", "INFO").upper()
        c.log_uid_plaintext = _bool(_env("LOG_UID_PLAINTEXT", "false"))

        c.backoffice_check_enabled = _bool(_env("BACKOFFICE_CHECK_ENABLED", "true"))
        c.notify_url = _env("NOTIFY_URL", "http://127.0.0.1:8123/api/webhook/evcc-push-CHANGE-ME")

        return c

    def log_startup(self):
        lines = [
            "effective configuration:",
            f"  alfen_host          = {self.alfen_host}",
            f"  alfen_username      = {self.alfen_username}",
            f"  alfen_password      = {'*' * 8}",
            f"  alfen_socket        = {self.alfen_socket}",
            f"  alfen_tls_verify    = {self.alfen_tls_verify}",
            f"  tag_wait_timeout_s  = {self.tag_wait_timeout}",
            f"  tag_poll_interval_s = {self.tag_poll_interval}",
            f"  login_retries       = {self.login_retries}",
            f"  login_retry_backoff = {self.login_retry_backoff}",
            f"  evcc_base_url       = {self.evcc_base_url}",
            f"  evcc_loadpoint_id   = {self.evcc_loadpoint_id}",
            f"  release_on_disconnect = {self.release_on_disconnect}",
            f"  on_unknown_tag      = {self.on_unknown_tag}",
            f"  default_vehicle     = {self.default_vehicle or '(none)'}",
            f"  mqtt_host           = {self.mqtt_host}:{self.mqtt_port}",
            f"  mqtt_username       = {self.mqtt_username or '(none)'}",
            f"  mqtt_topic_prefix   = {self.mqtt_topic_prefix}",
            f"  uid_vehicle_map     = {list(self.uid_vehicle_map.values())} ({len(self.uid_vehicle_map)} entries)",
            f"  dry_run             = {self.dry_run}",
            f"  log_level           = {self.log_level}",
            f"  backoffice_check    = {self.backoffice_check_enabled}",
            f"  notify_url          = {self.notify_url}",
        ]
        for line in lines:
            log.info(line)
