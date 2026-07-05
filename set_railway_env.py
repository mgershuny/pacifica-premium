#!/usr/bin/env python3
"""Read .env and set each variable on Railway"""
import subprocess, re, os

env_path = "/home/mgers/pacifica-premium/.env"
if not os.path.exists(env_path):
    print("No .env found")
    exit(1)

with open(env_path) as f:
    content = f.read()

# Parse key=value pairs, skip comments and blank lines
for line in content.splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        continue
    key, _, val = line.partition("=")
    key = key.strip()
    val = val.strip().strip('"').strip("'")
    if not key:
        continue
    # Skip if value looks like a redacted placeholder
    if val.startswith("***") or val == "PLACEHOLDER" or val.startswith("..."):
        print(f"SKIP {key} (redacted/placeholder)")
        continue
    result = subprocess.run(
        ["railway", "variable", "set", f"{key}={val}", "--skip-deploys"],
        capture_output=True, text=True, timeout=15,
        cwd="/home/mgers/pacifica-premium"
    )
    if result.returncode == 0:
        print(f"OK   {key}")
    else:
        print(f"FAIL {key}: {result.stderr.strip() or result.stdout.strip()}")

print("\nDone!")
