"""Load Home Assistant Supervisor options, then start the bridge."""

import json
import logging
import os
import pwd
import sys

import requests


LOG = logging.getLogger("ha_options")
SUPERVISOR_URL = "http://supervisor"


def _supervisor_get(path):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Home Assistant did not provide a Supervisor API token")
    response = requests.get(
        f"{SUPERVISOR_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=(3, 10),
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as err:
        # Include the failing endpoint and Supervisor response, but never log
        # request headers (which contain the Supervisor token).
        detail = " ".join(response.text.split())[:300]
        message = f"Supervisor request {path} failed with HTTP {response.status_code}"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from err
    try:
        return response.json()
    except ValueError as err:
        raise RuntimeError(f"Supervisor request {path} returned invalid JSON") from err


def _discover_evcc(options):
    if options.get("EVCC_BASE_URL"):
        return
    if not options.get("EVCC_AUTO_DISCOVERY", True):
        raise RuntimeError("Set EVCC_BASE_URL or enable EVCC_AUTO_DISCOVERY")

    try:
        self_response = _supervisor_get("/addons/self/info")
        self_info = self_response.get("data", self_response)
        own_slug = self_info.get("slug")
        response = _supervisor_get("/addons")
    except (requests.RequestException, RuntimeError) as err:
        raise RuntimeError(
            f"Could not read installed Home Assistant apps ({err}); "
            "set EVCC_BASE_URL manually if discovery is unavailable"
        ) from err
    addons = response.get("data", {}).get("addons", response.get("addons", []))
    candidates = [
        addon for addon in addons
        if "evcc" in (str(addon.get("slug", "")) + " " + str(addon.get("name", ""))).lower()
        and addon.get("slug") != own_slug
        and addon.get("state") in ("started", "stopped")
        and addon.get("installed", True)
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "Could not select exactly one installed EVCC app automatically; "
            "set EVCC_BASE_URL manually"
        )

    slug = candidates[0].get("slug")
    if not slug:
        raise RuntimeError("The discovered EVCC app has no Supervisor slug")
    # Supervisor app slugs include the repository prefix and match the internal
    # app DNS alias, with underscores replaced by hyphens.
    alias = slug.replace("_", "-")
    options["EVCC_BASE_URL"] = f"http://{alias}:7070"
    LOG.info("Using discovered EVCC app at %s", options["EVCC_BASE_URL"])


def _discover_mqtt(options):
    if options.get("MQTT_HOST"):
        # Explicit broker settings take precedence over Supervisor discovery.
        options.setdefault("MQTT_PORT", 1883)
        options.setdefault("MQTT_USERNAME", "")
        options.setdefault("MQTT_PASSWORD", "")
        LOG.info("Using manually configured MQTT broker")
        return
    if not options.get("MQTT_AUTO_DISCOVERY", True):
        raise RuntimeError("Set MQTT_HOST or enable MQTT_AUTO_DISCOVERY")

    try:
        response = _supervisor_get("/services/mqtt")
    except requests.RequestException as err:
        raise RuntimeError(
            "Could not discover a Home Assistant MQTT service; set MQTT_HOST "
            "and any required credentials manually"
        ) from err
    service = response.get("data", response)
    host = service.get("host")
    if not host:
        raise RuntimeError(
            "No MQTT service was found; set MQTT_HOST (and optional credentials) manually"
        )
    options["MQTT_HOST"] = host
    options["MQTT_PORT"] = service.get("port") or 1883
    options["MQTT_USERNAME"] = service.get("username") or ""
    options["MQTT_PASSWORD"] = service.get("password") or ""
    LOG.info("Using the MQTT service provided by Home Assistant")


def _resolve_connection_options(options):
    _discover_evcc(options)
    _discover_mqtt(options)


def _drop_privileges():
    """Run the long-lived bridge as its unprivileged service account."""
    if os.geteuid() != 0:
        return
    bridge_user = pwd.getpwnam("bridge")
    os.setgroups([])
    os.setgid(bridge_user.pw_gid)
    os.setuid(bridge_user.pw_uid)


def main():
    options_path = "/data/options.json"
    if os.path.isfile(options_path):
        with open(options_path, encoding="utf-8") as options_file:
            options = json.load(options_file)
        try:
            _resolve_connection_options(options)
        except (requests.RequestException, RuntimeError, ValueError) as err:
            sys.exit(f"[home-assistant] {err}")
        for key, value in options.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                os.environ[key] = json.dumps(value)
            elif isinstance(value, bool):
                os.environ[key] = str(value).lower()
            else:
                os.environ[key] = str(value)

    # Supervisor API credentials are only needed during option discovery.
    os.environ.pop("SUPERVISOR_TOKEN", None)
    _drop_privileges()
    os.execv(sys.executable, [sys.executable, "-u", "/app/main.py", *sys.argv[1:]])


if __name__ == "__main__":
    main()
