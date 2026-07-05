#!/usr/bin/env python3
import urllib.request, json, sys

with open("/tmp/gh_token.txt") as f:
    token = f.read().strip()

# Create repo
data = json.dumps({
    "name": "pacifica-premium",
    "description": "Pacifica Premium - Airport & Luxury Transportation",
    "private": False
}).encode()

req = urllib.request.Request("https://api.github.com/user/repos", data=data, method="POST")
req.add_header("Authorization", "Bearer " + token)
req.add_header("Content-Type", "application/json")

try:
    resp = urllib.request.urlopen(req)
    r = json.loads(resp.read())
    print("OK:", r["html_url"])
    sys.exit(0)
except urllib.error.HTTPError as e:
    err = json.loads(e.read())
    print("FAIL:", e.code, err.get("message", ""))
    sys.exit(1)
