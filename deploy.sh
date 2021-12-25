#!/usr/bin/env sh

# Abort on errors
set -e

# Build
yarn run build

# Navigate into the build output directory
cd dist

# Deploying to a custom domain
echo 'profile.hydev.org' > CNAME

git init
git add -A
git commit -m 'deploy'
git push -f git@github.com:hykilpikonna/home-page.git master:gh-pages
cd -
