#!/usr/bin/env sh

# Abort on errors
set -e

# Build
yarn run build

DEPL="/etc/nginx/hres/aza.moe"

# Update static files (since mv is way faster than cp, we use a tmp folder)
if [ -d "$DEPL.tmp1" ]; then
  echo "Deleting old tmp folder..."
  sudo rm -rf "$DEPL.tmp1"
fi
echo "Copying new files to tmp folder..."
cp -r dist "$DEPL.tmp1"
if [ -d "$DEPL" ]; then
  echo "Moving old production folder to to-delete location..."
  sudo mv "$DEPL" "$DEPL.del"
fi
echo "Moving new files to production folder..."
sudo mv "$DEPL.tmp1" "$DEPL"
if [ -d "$DEPL.del" ]; then
  echo "Deleting old production folder..."
  sudo rm -rf "$DEPL.del"
fi

# Reload nginx
sudo nginx -t && sudo systemctl reload nginx
