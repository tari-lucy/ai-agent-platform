#!/usr/bin/env python3
"""Publish the panel in nginx-proxy-manager from the server itself (run in your own terminal).

    python3 npm_publish_panel.py [--domain agents.tarigpt.ru] [--upstream 172.18.0.1:8765]

It talks to NPM's API on 127.0.0.1:81 only. The NPM login and the new basic-auth password are
typed here (no echo) and never printed, stored or sent anywhere but that local API. Creates, in
this order: a Let's Encrypt certificate for the domain, an access list (login + password at the
door; the panel's own login stays), and the proxy host with HTTPS forced. An existing proxy host
for the domain is never touched.
"""

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from typing import Any

API = "http://127.0.0.1:81/api"
ALLOW_ALL = [{"address": "all", "directive": "allow"}]


def call(
    method: str, path: str, token: str | None = None, body: object = None, timeout: int = 30
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get("error", {}).get("message", "")
        except ValueError:
            message = ""
        sys.exit(f"NPM refused {method} {path}: HTTP {error.code} {message}".strip())
    except OSError as error:
        sys.exit(f"NPM is not reachable on 127.0.0.1:81 ({type(error).__name__})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="agents.tarigpt.ru")
    parser.add_argument("--upstream", default="172.18.0.1:8765")
    parser.add_argument(
        "--fix-access",
        action="store_true",
        help="repair the existing access list: NPM turns a list without address rules into 403",
    )
    arguments = parser.parse_args()
    domain = arguments.domain.strip().lower()
    host, _, port = arguments.upstream.rpartition(":")
    if not domain or "/" in domain or not host or not port.isdigit():
        sys.exit("Bad --domain or --upstream.")

    email = input("NPM login (email): ").strip()
    secret = getpass.getpass("NPM password: ")
    token = call("POST", "/tokens", body={"identity": email, "secret": secret})["token"]
    del secret

    if arguments.fix_access:
        lists = [
            row
            for row in call("GET", "/nginx/access-lists", token)
            if row.get("name") == f"panel {domain}"
        ]
        if len(lists) != 1:
            sys.exit(f"Expected one access list named 'panel {domain}', found {len(lists)}.")
        # Only the address rules are sent: the login and password already stored are kept.
        call("PUT", f"/nginx/access-lists/{lists[0]['id']}", token, {"clients": ALLOW_ALL})
        print(
            f"Access list {lists[0]['id']} now allows every address (the password still applies)."
        )
        return

    for existing in call("GET", "/nginx/proxy-hosts", token):
        if domain in existing.get("domain_names", []):
            sys.exit(
                f"A proxy host for {domain} already exists (id {existing['id']}); not touched."
            )

    door_user = input("Login for the door (basic auth) on the new site: ").strip()
    door_password = getpass.getpass("Password for the door: ")
    if not door_user or len(door_password) < 12 or door_password != getpass.getpass("Repeat: "):
        sys.exit("Need a login, a password of 12+ characters, and the same password twice.")

    print("1/3 Let's Encrypt certificate (up to a few minutes) ...", flush=True)
    certificate = call(
        "POST",
        "/nginx/certificates",
        token,
        {"provider": "letsencrypt", "domain_names": [domain], "meta": {"dns_challenge": False}},
        timeout=300,
    )
    print(f"    certificate id {certificate['id']}")

    print("2/3 access list ...", flush=True)
    access = call(
        "POST",
        "/nginx/access-lists",
        token,
        {
            "name": f"panel {domain}",
            "satisfy_any": False,
            "pass_auth": False,  # the panel refuses requests that carry an Authorization header
            "items": [{"username": door_user, "password": door_password}],
            # NPM writes "deny all" after the rules: without "allow all" the site answers 403.
            "clients": ALLOW_ALL,
        },
    )
    del door_password
    print(f"    access list id {access['id']}")

    print("3/3 proxy host ...", flush=True)
    created = call(
        "POST",
        "/nginx/proxy-hosts",
        token,
        {
            "domain_names": [domain],
            "forward_scheme": "http",
            "forward_host": host,
            "forward_port": int(port),
            "certificate_id": certificate["id"],
            "access_list_id": access["id"],
            "ssl_forced": True,
            "http2_support": True,
            "block_exploits": True,
            "caching_enabled": False,
            "allow_websocket_upgrade": True,
            "hsts_enabled": False,
            "hsts_subdomains": False,
            "enabled": True,
            "meta": {},
            "locations": [],
        },
    )
    print(f"    proxy host id {created['id']}")
    print(f"Done: https://{domain}  (first the door login, then the panel's own login)")


if __name__ == "__main__":
    main()
