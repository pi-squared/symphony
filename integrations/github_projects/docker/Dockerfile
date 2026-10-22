# Supply a build context containing the complete pinned standalone package as codex/.
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git python3 gh && rm -rf /var/lib/apt/lists/*
COPY codex/ /opt/codex/
RUN chmod -R a+rX /opt/codex && mkdir -p /home/symphony/.codex /home/symphony/.config/gh && chmod 1777 /home/symphony
COPY seed_skills.py /tmp/seed_skills.py
RUN python3 /tmp/seed_skills.py && rm /tmp/seed_skills.py && chmod -R a+rX /opt/codex/packaged-skills
ENV PATH="/opt/codex/bin:${PATH}"
WORKDIR /workspace

ENTRYPOINT []
