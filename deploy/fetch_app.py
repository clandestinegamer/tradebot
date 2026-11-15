"""Copy the newest Android app from the GitHub release "android-latest" into data/app/.

Usage: python3 fetch_app.py OWNER/REPO OUT_DIR  < token-file
Standard library only, so it runs on a bare Ubuntu server.
"""
import hashlib
import json
import os
import sys
import urllib.request

API = "https://api.github.com"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None                              # handle redirects ourselves (see download)


def call(url, token, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": accept,
                                               "User-Agent": "tradebot-updater"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            # Asset downloads redirect to storage that must NOT receive the GitHub token.
            with urllib.request.urlopen(urllib.request.Request(
                    e.headers["Location"], headers={"User-Agent": "tradebot-updater"}), timeout=120) as r:
                return r.read()
        raise


def main():
    repo, out = sys.argv[1], sys.argv[2]
    token = sys.stdin.read().strip()
    rel = json.loads(call(f"{API}/repos/{repo}/releases/tags/android-latest", token))
    assets = {a["name"]: a["url"] for a in rel.get("assets", [])}
    if "version.json" not in assets or "TradeBot.apk" not in assets:
        return                                   # no app build published yet
    remote = json.loads(call(assets["version.json"], token, "application/octet-stream"))
    vfile = os.path.join(out, "version.json")
    try:
        with open(vfile) as f:
            if json.load(f).get("versionCode") == remote["versionCode"]:
                return                           # already have this version
    except (OSError, ValueError):
        pass
    apk = call(assets["TradeBot.apk"], token, "application/octet-stream")
    if hashlib.sha256(apk).hexdigest() != remote["sha256"]:
        sys.exit("downloaded APK doesn't match version.json; skipped")
    os.makedirs(out, exist_ok=True)
    for name, data in (("TradeBot.apk", apk), ("version.json", json.dumps(remote).encode())):
        tmp = os.path.join(out, name + ".tmp")   # APK first, so version.json never points at a missing file
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(out, name))
    print(f"app update {remote.get('versionName')} ready for phones")


if __name__ == "__main__":
    main()
