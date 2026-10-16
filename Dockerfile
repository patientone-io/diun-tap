FROM python:3.12-slim

# Host docker socket group id (default 999 = Debian docker group).
# Override at build time: docker build --build-arg DOCKER_GID=$(stat -c %g /var/run/docker.sock)
ARG DOCKER_GID=999

WORKDIR /app

# Install Docker CLI, Docker Compose plugin and OpenSSH client.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    openssh-client \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 diuntap \
    && (getent group "${DOCKER_GID}" || groupadd -g "${DOCKER_GID}" docker-host) \
    && useradd -u 1000 -g diuntap -d /home/diuntap -m -s /bin/bash diuntap \
    && usermod -aG "${DOCKER_GID}" diuntap \
    && mkdir -p /srv/docker /config /data /home/diuntap/.ssh \
    && chown -R 1000:1000 /app /srv/docker /config /data /home/diuntap

COPY --chown=1000:1000 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=1000:1000 main.py app.py config.py models.py security.py dedup.py \
    github_client.py telegram_client.py polling.py docker_local.py remote_ssh.py \
    errors.py mappings.py i18n.py formatters.py filters.py ./

EXPOSE 5000

ENV PYTHONUNBUFFERED=1 \
    DOCKER_STACKS_DIR=/srv/docker \
    DIUNTAP_STATE_DIR=/data \
    HOME=/home/diuntap

USER diuntap

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
