FROM python:3.12-slim

WORKDIR /app

# Install Docker CLI, Docker Compose Plugin, and OpenSSH client
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
    && useradd -u 1000 -g diuntap -d /home/diuntap -m -s /bin/bash diuntap \
    && mkdir -p /srv/docker /config \
    && chown -R 1000:1000 /app /srv/docker /config

COPY --chown=1000:1000 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=1000:1000 app.py mappings.py i18n.py formatters.py ./

EXPOSE 5000

ENV PYTHONUNBUFFERED=1 \
    DOCKER_STACKS_DIR=/srv/docker

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
