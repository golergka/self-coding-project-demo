FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
ARG CODEX_VERSION=0.144.1
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js install -g @openai/codex@${CODEX_VERSION}
RUN pip install --no-cache-dir https://github.com/golergka/coloph-agent-runtime/releases/download/v0.3.0/coloph_agent_runtime-0.3.0-py3-none-any.whl
RUN useradd --create-home agent && mkdir /workspace && chown agent:agent /workspace
COPY --chmod=755 codex_entry.py /usr/local/bin/self-coding-codex
USER agent
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "coloph_agent_runtime.worker"]
