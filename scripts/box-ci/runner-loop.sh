#!/usr/bin/env bash
# Ephemeral self-hosted runner loop for jaydenpiao/claude-duel (labels self-hosted,box).
# Runs jobs as the unprivileged user ghrunner with a clean env (no OPENAI_API_KEY). Usage: runner-loop.sh [N] (N = 2, 3 for extra runners). Stop: kill $(cat /workspace/box-ci/runner-loop*.pid); then gh api -X DELETE the runners
set -u
N=${1:-}
echo $$ > /workspace/box-ci/runner-loop$N.pid
R=/home/ghrunner/runner$N
while true; do
  tok=$(gh api -X POST repos/jaydenpiao/claude-duel/actions/runners/registration-token -q .token) || { sleep 60; continue; }
  sudo -u ghrunner rm -rf $R/_work $R/.runner $R/.credentials $R/.credentials_rsaparams
  sudo -u ghrunner env -i HOME=/home/ghrunner XDG_RUNTIME_DIR=/home/ghrunner/.run DOCKER_HOST=unix:///home/ghrunner/.run/docker.sock DUEL_DOCKER="docker -H unix:///home/ghrunner/.run/docker.sock" PATH=/home/ghrunner/.local/bin:/usr/local/bin:/usr/bin:/bin bash -c \
    "cd $R && ./config.sh --unattended --ephemeral --replace --url https://github.com/jaydenpiao/claude-duel --token \"\$1\" --name box-runner$N --labels self-hosted,box --work _work >/dev/null && ./run.sh" _ "$tok"
  unset tok
  echo "$(date -Is) runner exited $?"
  sleep 5
done
