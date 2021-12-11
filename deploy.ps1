# Build
yarn run build

# Navigate into the build output directory
cd dist

# Deploying to a custom domain
echo 'profile.hydev.org' > CNAME

git init
git add -A
git commit -m 'deploy'
git push -f https://github.com/hykilpikonna/home-page.git master:gh-pages
cd ..
