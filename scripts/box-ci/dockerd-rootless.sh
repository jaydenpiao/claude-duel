#!/usr/bin/env bash
# Start rootless dockerd for ghrunner in a delegated cgroup (so --memory/--pids-limit work). Run as root via sudo.
set -eu
mkdir -p /sys/fs/cgroup/ghrunner
echo "+cpu +memory +pids +io" > /sys/fs/cgroup/ghrunner/cgroup.subtree_control 2>/dev/null || true
chown -R ghrunner:ghrunner /sys/fs/cgroup/ghrunner
sudo -u ghrunner bash -c 'mkdir -p ~/.run && chmod 700 ~/.run'
setsid bash -c 'echo $$ > /sys/fs/cgroup/ghrunner/cgroup.procs; exec sudo -u ghrunner env -i HOME=/home/ghrunner XDG_RUNTIME_DIR=/home/ghrunner/.run PATH=/usr/share/docker.io/contrib:/usr/sbin:/usr/bin:/bin dockerd-rootless.sh --exec-opt native.cgroupdriver=cgroupfs' >> /home/ghrunner/.run/dockerd.log 2>&1 < /dev/null &
