"""One-time LinkedIn connection for posting as a member (3-legged OAuth).

Before running (once, at https://www.linkedin.com/developers/apps):
  1. Create an app. LinkedIn asks for a company Page to attach it to; any Page you manage works.
  2. Products tab: add "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect".
  3. Auth tab: add the redirect URL  http://localhost:8767/linkedin/callback
  4. Put the app's Client ID and Client Secret in .env as
     LINKEDIN_CLIENT_ID=...   LINKEDIN_CLIENT_SECRET=...

Then: python -m autopilot.connect
It opens the browser, you sign in and allow posting, and it writes to .env (never printed):
  LINKEDIN_ACCESS_TOKEN, LINKEDIN_PERSON_URN, LINKEDIN_TOKEN_EXPIRES
LinkedIn member tokens last about 60 days; run this again before the expiry date (the job warns a week ahead).
"""
import argparse
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, timedelta

from . import core as li
from .env import require, set_env

PORT = 8767
REDIRECT = f"http://localhost:{PORT}/linkedin/callback"
SCOPES = "openid profile w_member_social"


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    client_id, secret = require("LINKEDIN_CLIENT_ID"), require("LINKEDIN_CLIENT_SECRET")
    state, result = secrets.token_urlsafe(16), {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>LinkedIn connected. You can close this tab.</h2>" if "code" in q
                             else b"<h2>LinkedIn didn't connect. Check the terminal.</h2>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state, "scope": SCOPES})
    print("Opening LinkedIn in your browser. If it doesn't open, paste this link:\n" + url)
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if result.get("state") != state or "code" not in result:
        sys.exit(f"LinkedIn sign-in failed: {result.get('error_description') or result.get('error') or 'state mismatch'}")

    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
                                   "client_id": client_id, "client_secret": secret}).encode()
    req = urllib.request.Request("https://www.linkedin.com/oauth/v2/accessToken", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    token = json.loads(urllib.request.urlopen(req, timeout=30).read())
    access = token["access_token"]
    expires = date.today() + timedelta(seconds=int(token.get("expires_in", 5184000)))
    me = li.LinkedIn(access, "").me()
    set_env("LINKEDIN_ACCESS_TOKEN", access)
    set_env("LINKEDIN_PERSON_URN", f"urn:li:person:{me['sub']}")
    set_env("LINKEDIN_TOKEN_EXPIRES", expires.isoformat())
    print(f"Connected as {me.get('name', 'this member')}. Token saved to .env; it expires {expires}.")


if __name__ == "__main__":
    main()
