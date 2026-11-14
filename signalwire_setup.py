"""Puts the SignalWire side of the pumpkin in place, from a space, project ID,
and API token.

On startup it makes sure an SWML webhook resource points at this agent (creating
or correcting it), finds the address calls reach it by, and mints guest tokens
for the pumpkin page that can dial that one address and nothing else.
"""

import hashlib
import hmac
import logging
import os
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger("signalwire_setup")

RESOURCE_NAME = os.environ.get("PUMPKIN_RESOURCE_NAME", "scarecrow")
TOKEN_LIFETIME_SECONDS = 2 * 60 * 60


def derive_basic_auth():
    """Fill in SWML_BASIC_AUTH_* from the API token when they aren't set.

    The password is derived, not random, so it's the same across restarts:
    a call already in progress keeps working after a code reload.
    """
    os.environ.setdefault("SWML_BASIC_AUTH_USER", "pumpkin")
    api_token = os.environ.get("SIGNALWIRE_API_TOKEN")
    if api_token and not os.environ.get("SWML_BASIC_AUTH_PASSWORD"):
        os.environ["SWML_BASIC_AUTH_PASSWORD"] = hmac.new(
            api_token.encode(), b"scarecrow swml basic auth", hashlib.sha256
        ).hexdigest()[:32]


def video_channel(address):
    """The address to dial, on its video channel so the pumpkin can see."""
    channels = address["channels"]
    if "video" in channels:
        return channels["video"]
    audio = urlsplit(channels["audio"])
    query = dict(parse_qsl(audio.query), channel="video")
    return urlunsplit(audio._replace(query=urlencode(query)))


class SignalWireSetup:
    def __init__(self, space, project_id, api_token, agent_url):
        self.space = space
        self.agent_url = agent_url
        self.session = requests.Session()
        self.session.auth = (project_id, api_token)
        self._address = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, route):
        """None when the SignalWire settings aren't all present."""
        names = ["SIGNALWIRE_SPACE", "SIGNALWIRE_PROJECT_ID", "SIGNALWIRE_API_TOKEN",
                 "SWML_PROXY_URL_BASE", "SWML_BASIC_AUTH_PASSWORD"]
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            log.warning("SignalWire setup skipped; missing %s", ", ".join(missing))
            return None
        # SignalWire fetches the agent with the basic-auth pair in the URL.
        base = urlsplit(os.environ["SWML_PROXY_URL_BASE"])
        userinfo = f"{os.environ['SWML_BASIC_AUTH_USER']}:{os.environ['SWML_BASIC_AUTH_PASSWORD']}"
        agent_url = urlunsplit((base.scheme, f"{userinfo}@{base.netloc}", base.path.rstrip("/") + route, "", ""))
        return cls(
            os.environ["SIGNALWIRE_SPACE"],
            os.environ["SIGNALWIRE_PROJECT_ID"],
            os.environ["SIGNALWIRE_API_TOKEN"],
            agent_url,
        )

    # ------------------------------------------------------------ REST

    def _call(self, method, path, **kwargs):
        url = path if path.startswith("https://") else f"https://{self.space}/api/fabric/{path}"
        response = self.session.request(method, url, timeout=15, **kwargs)
        if not response.ok:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    def _list(self, path):
        page = self._call("GET", path)
        while True:
            yield from page.get("data", [])
            next_url = page.get("links", {}).get("next")
            if not next_url:
                return
            page = self._call("GET", next_url)

    # ------------------------------------------------------------ setup

    def ensure(self):
        """Create or correct the resource and return its address. Safe to repeat."""
        with self._lock:
            if self._address:
                return self._address

            resource = next(
                (r for r in self._list("resources/swml_webhooks") if r["swml_webhook"]["name"] == RESOURCE_NAME),
                None,
            )
            if resource is None:
                resource = self._call("POST", "resources/swml_webhooks", json={
                    "name": RESOURCE_NAME,
                    "used_for": "calling",
                    "primary_request_url": self.agent_url,
                    "primary_request_method": "POST",
                })
                log.info("Created SWML webhook resource %r", RESOURCE_NAME)
            elif resource["swml_webhook"]["primary_request_url"] != self.agent_url:
                self._call("PATCH", f"resources/swml_webhooks/{resource['id']}", json={
                    "primary_request_url": self.agent_url,
                    "primary_request_method": "POST",
                })
                log.info("Updated SWML webhook resource %r to point at this agent", RESOURCE_NAME)

            addresses = list(self._list(f"resources/swml_webhooks/{resource['id']}/addresses"))
            if not addresses:
                raise RuntimeError(f"SWML webhook {RESOURCE_NAME!r} has no address yet")
            self._address = addresses[0]
            log.info("Pumpkin answers at %s", video_channel(self._address))
            return self._address

    def ensure_in_background(self):
        """Run setup at startup without blocking it; errors surface in the log,
        and the first token request tries again."""
        def run():
            try:
                self.ensure()
            except Exception as e:
                log.error("SignalWire setup failed: %s", e)
        threading.Thread(target=run, daemon=True).start()

    def guest_token(self):
        """A short-lived token that can dial the pumpkin and nothing else."""
        address = self.ensure()
        expires = int(time.time()) + TOKEN_LIFETIME_SECONDS
        body = self._call("POST", "guests/tokens", json={
            "allowed_addresses": [address["id"]],
            "expire_at": expires,
        })
        return {"token": body["token"], "expiresAt": expires * 1000, "destination": video_channel(address)}
