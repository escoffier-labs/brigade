# Official Linux amd64 manifests, verified before candidate CI definition.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b
FROM node:22-bookworm-slim@sha256:efd0ab5780c2d9ab1f0f869571a00d5edb17793bff4cce4a2792e3eb0ffc7562 AS node
FROM ${PYTHON_IMAGE}
# Only image preparation has network access. Test containers use network none.
# git supports owner fixtures; ps and Node support existing transport tests.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends git procps \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir 'pytest==9.1.1' \
    && node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'
COPY supervisor.py /opt/supervisor.py
ENTRYPOINT ["python", "/opt/supervisor.py"]
