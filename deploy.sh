#!/bin/bash
# Create GitHub repo and push pacifica-premium
set -e
TOKEN="github...PEoy"

echo "=== Creating repo ==="
RESP=$(curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/user/repos \
  -d '{"name":"pacifica-premium","description":"Pacifica Premium - Airport & Luxury Transportation","private":false}')
echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print('HTML_URL:', d.get('html_url', d.get('message','FAILED: see full output')))"

echo ""
echo "=== Setting git remote ==="
cd /home/mgers/pacifica-premium
git remote remove origin 2>/dev/null || true
git remote add origin https://mgershuny:$TOKEN@github.com/mgershuny/pacifica-premium.git

echo "=== Staging files ==="
git add -A

echo "=== Committing ==="
git commit -m "Initial commit - Pacifica Premium landing page + Flask booking app"

echo "=== Pushing ==="
git push -u origin main || git push -u origin master

echo ""
echo "=== Done! ==="