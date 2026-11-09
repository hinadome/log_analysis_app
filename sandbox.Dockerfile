# ScriptEngine sandbox image — what scripts run inside when
# SCRIPTENGINE_DOCKER_SANDBOX=1. Deliberately separate from the app Dockerfile:
# that one ships ScriptEngine itself, this one ships only the interpreters
# scripts may need (python3, bash, node, ruby, perl), nothing else.
#
# Built automatically by run.sh when the sandbox is enabled, or manually:
#   docker build -f sandbox.Dockerfile -t scriptengine-sandbox .
#
# Scripts needing extra libraries (pip packages, CLI tools): extend this
# image and point SCRIPTENGINE_SANDBOX_IMAGE at your tag — the executor runs
# whatever command the manifest specifies inside it.

FROM python:3.12-slim

# bash ships with the base image; add the other sample-script interpreters.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ruby perl \
    && rm -rf /var/lib/apt/lists/*

# The container runs with --network none, a memory cap and the host user's
# uid:gid (set per-run by the executor) — nothing to configure here.
WORKDIR /work
