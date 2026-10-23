#!/usr/bin/env bash
# Publish the run's private results into the DATA repo clone (./data),
# conflict-proof: snapshot files, realign to the live remote head, copy
# back, commit, push with retries. Never force-pushes.
set -u
cd "${DATA_DIR:-data}" || { echo "data clone missing"; exit 1; }
git config user.name  "domain-finder-bot"
git config user.email "bot@users.noreply.github.com"

FILES="${DATA_FILES:-state/results.json state/alerted.json state/run.log state/ledger-backup}"
BRANCH=$(git rev-parse --abbrev-ref HEAD)

SNAP=$(mktemp -d)
for f in $FILES; do
  if [ -e "$f" ]; then
    mkdir -p "$SNAP/$(dirname "$f")"
    cp -r "$f" "$SNAP/$f"
  fi
done

for attempt in 1 2 3; do
  if ! git fetch origin "$BRANCH"; then sleep 10; continue; fi
  git reset --hard >/dev/null
  git checkout -B "$BRANCH" FETCH_HEAD >/dev/null 2>&1
  for f in $FILES; do
    if [ -e "$SNAP/$f" ]; then
      mkdir -p "$(dirname "$f")"
      rm -rf "$f"
      cp -r "$SNAP/$f" "$f"
    fi
  done
  for f in $FILES; do
    if [ -e "$f" ]; then git add "$f"; fi
  done
  if git diff --cached --quiet; then
    echo "nothing new to publish"
    exit 0
  fi
  git commit -m "${COMMIT_MSG:-daily results}" >/dev/null
  if git push origin "$BRANCH"; then
    echo "published on attempt $attempt"
    exit 0
  fi
  echo "push raced; retrying"
  sleep $((attempt * 5))
done
echo "could not publish after 3 attempts"
exit 1
