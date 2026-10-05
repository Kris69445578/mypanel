#!/usr/bin/env bash
# Run on YOUR computer, inside this folder:  ./publish.sh <github-username> [repo-name]
set -e
USER_NAME="${1:?usage: ./publish.sh <github-username> [repo-name]}"
REPO="${2:-mypanel}"
sed -i.bak "s/YOUR_GITHUB_USERNAME/$USER_NAME/g; s#/mypanel\(\.git\|/main\)#/$REPO\1#g" install.sh README.md && rm -f install.sh.bak README.md.bak
git init -q -b main 2>/dev/null || git init -q
git add -A && git commit -qm "MyPanel" || true
if command -v gh >/dev/null; then
  gh repo create "$USER_NAME/$REPO" --public --source=. --push
else
  git remote add origin "https://github.com/$USER_NAME/$REPO.git" 2>/dev/null || true
  echo "Create an empty repo named '$REPO' on GitHub, then run:  git push -u origin main"
fi
echo
echo "Install on any Ubuntu server with:"
echo "curl -sSL https://raw.githubusercontent.com/$USER_NAME/$REPO/main/install.sh | sudo REPO_URL=https://github.com/$USER_NAME/$REPO.git bash"
