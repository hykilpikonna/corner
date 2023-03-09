#!/usr/bin/env sh

# Abort on errors
set -e

# Build
yarn run build

DEPL="/etc/nginx/hres/aza.moe"

# Update static files (since mv is way faster than cp, we use a tmp folder)
if [ -d "$DEPL.tmp1" ]; then
  sudo rm -rf "$DEPL.tmp1"
fi
cp -r dist "$DEPL.tmp1"
if [ -d "$DEPL" ]; then
  sudo mv "$DEPL" "$DEPL.del"
fi
sudo mv "$DEPL.tmp1" "$DEPL"
if [ -d "$DEPL.del" ]; then
  sudo rm -rf "$DEPL.del"
fi

# Reload nginx
sudo nginx -t && sudo systemctl restart nginx
