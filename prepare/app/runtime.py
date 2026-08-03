"""Runtime integration helpers shared by CLI and Web."""

import json
import os
import urllib.parse
import urllib.request


def trigger_navidrome_rescan(client: str = "music-tools") -> tuple[bool, str]:
    base_url = os.environ.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
    username = os.environ.get("NAVIDROME_USER", "")
    password = os.environ.get("NAVIDROME_PASSWORD", "")
    if not username or not password:
        return False, "NAVIDROME_USER/NAVIDROME_PASSWORD are not configured"
    query = urllib.parse.urlencode({
        "u": username,
        "p": password,
        "v": "1.16.1",
        "c": client,
        "f": "json",
    })
    try:
        with urllib.request.urlopen(f"{base_url}/rest/startScan?{query}", timeout=10) as response:
            payload = json.loads(response.read())
        status = payload.get("subsonic-response", {}).get("status")
        if status != "ok":
            return False, json.dumps(payload)
        return True, "scan requested"
    except Exception as exc:
        return False, str(exc)

