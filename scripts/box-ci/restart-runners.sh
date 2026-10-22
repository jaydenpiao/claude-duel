#!/usr/bin/env bash
# Restart the three ephemeral box runners (kills any running job).
cd /workspace/box-ci
for f in runner-loop*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; done
sudo pkill -u ghrunner -f Runner.Listener; sudo pkill -u ghrunner -f Runner.Worker; sleep 3
for id in $(gh api repos/jaydenpiao/claude-duel/actions/runners -q '.runners[]|select(.name|startswith("box-"))|.id'); do
  gh api -X DELETE "repos/jaydenpiao/claude-duel/actions/runners/$id" >/dev/null 2>&1
done
for n in "" 2 3; do nohup /workspace/box-ci/runner-loop.sh $n >> /workspace/box-ci/runner$n.log 2>&1 & done
