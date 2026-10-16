<div align="center">
  <img src="assets/logo.png" alt="DiunTap Logo" width="130" height="130" />
  <h1>DiunTap</h1>
  <p>
    <b>Interactive 1-click Docker updates with GitHub changelogs, healthchecks & instant rollback for <a href="https://github.com/crazy-max/diun">DIUN</a>.</b><br />
    <i>Interaktywne aktualizacje kontenerów Docker 1 kliknięciem, changelogi z GitHuba, automatyczny healthcheck i natychmiastowy rollback dla DIUN.</i>
  </p>

  <p>
    <a href="#-english"><b>🇬🇧 English</b></a> •
    <a href="#-polski"><b>🇵🇱 Polski</b></a>
  </p>

  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT" /></a>
    <img src="https://img.shields.io/badge/Python-3.12-blue.svg" alt="Python 3.12" />
    <img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg" alt="FastAPI" />
    <img src="https://img.shields.io/badge/Docker-Compose-2496ED.svg" alt="Docker Compose" />
  </p>
</div>

---

<div align="center">
  <img src="assets/telegram-preview.png" alt="DiunTap Telegram Interactive Workflow" width="100%" style="max-width: 900px; border-radius: 10px;" />
</div>

---

## 🇬🇧 English

### 💡 What is DIUN and Why DiunTap?

> [!NOTE]
> **What is DIUN?**  
> [DIUN (Docker Image Update Notifier)](https://crazymax.dev/diun/) created by [CrazyMax](https://github.com/crazy-max) is a lightweight, popular open-source tool that monitors Docker registries (Docker Hub, GHCR, Quay) and sends alerts whenever an image is updated.

* **The problem with Watchtower:** Automated unattended upgrades can break critical services or homelab databases at 3 AM. No human approval, no changelog, and notifications only arrive after the service goes down.
* **The limitation of DIUN:** DIUN is a fantastic image update detector, but by design it only sends passive text notifications. It cannot trigger updates and does not fetch GitHub release notes.
* **The DiunTap Solution:** **Human-in-the-loop control.** When DIUN detects a new image, DiunTap fetches the release notes from GitHub, delivers an interactive notification to your Telegram with inline action buttons, and lets you update your service with a single tap from your phone. If anything goes wrong, a 1-click rollback restores the previous working version instantly.

---

### 🏗️ How It Works

```text
┌────────────────────────────────────────────────────────┐
│             DIUN (Image Update Detector)               │
│  Scans registries (Docker Hub, GHCR, Quay, etc.)       │
└──────────────────────────┬─────────────────────────────┘
                           │ Webhook POST /webhook/diun
                           │ (optional X-Diun-Secret auth)
                           ▼
┌────────────────────────────────────────────────────────┐
│                        DiunTap                         │
│                                                        │
│  1. Verifies webhook secret (if configured)            │
│  2. Fetches Release Notes from GitHub API              │
│  3. Identifies Compose stack via labels or directory   │
│  4. Formats Telegram HTML with <blockquote expandable> │
│  5. Listens for button taps (LAN Long Polling)         │
└──────────────────────────┬─────────────────────────────┘
                           │ Telegram Bot API
                           ▼
┌────────────────────────────────────────────────────────┐
│               Your Telegram (Phone / Desktop)          │
│                                                        │
│  🚀 Container Update: navidrome                        │
│  📋 Changelog: v0.55.0 (collapsible quote)             │
│  [ 🚀 Update Now ]  [ ❌ Skip ]                        │
│  [ 🌐 Open Release on GitHub ]                         │
└──────────────────────────┬─────────────────────────────┘
                           │ Tap: [ 🚀 Update Now ]
                           ▼
┌────────────────────────────────────────────────────────┐
│                        DiunTap                         │
│                                                        │
│  1. Tags current images as restore points              │
│     (:diun-backup-<service>-<timestamp>)   │
│  2. Executes: docker compose pull && up -d             │
│  3. Polls health until stable (or timeout)             │
│  4. If healthy: ✅ Success! [ ⏪ Rollback ]            │
│  5. If failed:  ⚠️ last 20 log lines + auto-rollback   │
│     attempt + [ ⏪ Rollback ]                           │
└────────────────────────────────────────────────────────┘
```

**Code layout:** the application entrypoint is `main.py` (FastAPI app, webhook, Telegram polling, Docker/SSH orchestration). `app.py` is a backward-compatible shim (`from main import app`, so `uvicorn app:app` keeps working). Supporting modules: `config.py` (settings), `models.py` (payload validation + opaque button-action store), `security.py` (input allow-lists, auth), `docker_local.py` (Compose update/rollback/health), `remote_ssh.py` (validated SSH), `github_client.py`, `telegram_client.py`, `polling.py`, `dedup.py`, `errors.py`.

---

### ✨ Features

* 🚀 **Human-in-the-Loop 1-Click Updates:** Decide exactly when to update your containers with a single tap from your phone or desktop.
* 📋 **Smart GitHub Changelogs with Expandable Quotes:** Formats GitHub Releases markdown into Telegram-native `<blockquote expandable>` blocks. Clean typography, bold headers, clickable PR links, and a collapsible quote so long changelogs never clutter your chat.
* 🎯 **Dual Stack Discovery:** Automatically identifies the target Compose stack and working directory directly from Docker container labels (`com.docker.compose.project`), with static filesystem directory scan as a reliable fallback.
* 🛡️ **Notification Deduplication:** Bounded `TTLCache` debounce window prevents duplicate alerts when DIUN triggers concurrent webhooks for the same tag — without leaking memory over time.
* 📌 **Pin / Freeze:** `EXCLUDE_IMAGES` (wildcard patterns) and `PINNED_STACKS` (exact names) silently skip notifications for containers you want to keep on a specific version.
* 🔒 **100% LAN & Zero-Trust Friendly (Long Polling):** Receives Telegram button clicks using built-in Long Polling. **Zero open ports, zero port-forwarding, and no reverse proxies or Cloudflare tunnels required.**
* 🔑 **Optional Webhook Authentication:** Set `DIUN_WEBHOOK_SECRET` and DIUN signs each delivery with header `X-Diun-Secret` (or `?token=`). Wrong secret → `401`. Unset → legacy mode with a clear `WARNING` in logs (smooth migration).
* 👥 **Group-Safe Telegram Auth:** Only `from_user.id` is verified against `TELEGRAM_ALLOWED_USER_IDS` (falls back to `TELEGRAM_CHAT_ID`). Safe to use the bot in groups — strangers tapping your buttons get `Unauthorized`.
* 🩺 **Post-Update Healthcheck & Auto-Diagnostics:** Polls container state until stable (or timeout). If a container exits or reports `unhealthy`, DiunTap captures the last 20 lines of logs, sends them to Telegram and attempts an automatic rollback.
* ⏪ **1-Click Rollback:** Before pulling, each selected running image ID gets a timestamped backup (local tags include a reference hash; newest 3 per original reference, multi-service aware). Rollback restores with `up -d --pull never`, so a broken `:latest` is never re-pulled by accident.
* 🧩 **Modular Hardened Codebase:** Strict input allow-lists (no shell injection into SSH), enforced subprocess timeouts, non-blocking filesystem scans, serialized per-stack updates (double-tap safe).
* 🌐 **Multi-Node Ready (Optional):** Supports local Docker Compose stacks out-of-the-box, with built-in optional support for remote nodes via SSH (e.g. Podman Quadlets or remote VPS). Remote commands run with `StrictHostKeyChecking=yes`.
* 🌍 **Localization:** Built-in support for English (`LANGUAGE=en`) and Polish (`LANGUAGE=pl`).
* 📁 **Extensible Image Mappings:** Includes built-in mappings for dozens of popular homelab services, with support for custom overrides via `mappings.yaml`.

---

### Update and restore behavior

DiunTap updates only services whose complete image repository and configured tag match the notification. It does not change Compose or Quadlet files. To move from a pinned `v1` to `v2`, edit the configuration first. Digest-pinned services are skipped. Unrelated Compose services are not pulled or recreated.

A successful health check requires two consecutive healthy samples; `starting` is still pending. The default observation window is 60 seconds. Healthchecked containers get at least 60 seconds even with the legacy `HEALTHCHECK_WAIT_SECONDS=6`. A timeout or unavailable status is inconclusive: it produces a pending message without automatic rollback. Confirmed failures (`unhealthy`, `exited`, `dead`, `restarting`, or a failed remote unit), or a failed startup command, trigger an automatic rollback. Rollback restores container images, not application data or database migrations.

Telegram actions and the latest restore point per target live in SQLite. Mount `diuntap-state:/data` as shown below; outside Docker, set `DIUNTAP_STATE_DIR` (default `./data`). New buttons survive restarts for up to seven days, subject to the 4096-action limit. Buttons sent by older versions cannot be recovered. Rollback requires a restore point created by this version and restores the latest recorded update for the target.

For remote Quadlets, `ContainerName=` must match the service basename. The default pull policy (`missing`), explicit `Pull=missing`, and `Pull=never` are supported without requiring edits to existing units. DiunTap pulls explicitly before restarting. The loaded unit must use the same repository and tag (Docker Hub prefixes are normalized); `always` and `newer` are rejected because they can overwrite a restored local image. See [Podman Quadlet documentation](https://docs.podman.io/en/stable/markdown/podman-systemd.unit.5.html).

### 🚀 Quickstart

Deploy DiunTap using Docker Compose in seconds. **The container runs as non-root (`USER diuntap`)** and needs the host docker-socket group — see step 0.

#### 0. Docker socket group (required once per host)

```bash
stat -c %g /var/run/docker.sock   # e.g. 999 on Debian, 998 elsewhere
echo "DOCKER_GID=999" >> .env     # use YOUR number
```

If the number is wrong the container logs a clear `Permission denied on /var/run/docker.sock` hint instead of talking to Docker.

#### Option A: All-in-One Compose (DiunTap + DIUN together)

If you do not have DIUN running yet, deploy both services together in a single `compose.yaml`:

```yaml
services:
  diun-tap:
    image: ghcr.io/patientone-io/diun-tap:latest
    container_name: diun-tap
    restart: unless-stopped
    cap_drop:
      - ALL
    group_add:
      - ${DOCKER_GID:-999} # must match `stat -c %g /var/run/docker.sock`
    ports:
      - "5000:5000"
    env_file:
      - .env
    environment:
      - TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
      - TELEGRAM_CHAT_ID=123456789
      - TELEGRAM_ALLOWED_USER_IDS=123456789 # optional, recommended for groups
      - DIUN_WEBHOOK_SECRET=your_secret_here # optional, recommended
      - GITHUB_TOKEN= # optional (increases rate limit from 60 to 5,000 req/h)
      - LANGUAGE=en
      - DOCKER_STACKS_DIR=/srv/docker # Directory where your stacks are stored
      - DOCKER_GID=${DOCKER_GID:-999}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # 1:1 mount matching DOCKER_STACKS_DIR
      - diuntap-state:/data

  diun:
    image: crazymax/diun:latest
    container_name: diun
    command: serve
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./diun-data:/data
    environment:
      - "TZ=UTC"
      - "DIUN_WATCH_SCHEDULE=0 */6 * * *"
      - "DIUN_PROVIDERS_DOCKER=true"
      - "DIUN_PROVIDERS_DOCKER_WATCHBYDEFAULT=true"
      - "DIUN_NOTIF_WEBHOOK_ENDPOINT=http://diun-tap:5000/webhook/diun"
      - "DIUN_NOTIF_WEBHOOK_METHOD=POST"
      - "DIUN_NOTIF_WEBHOOK_HEADERS=X-Diun-Secret: your_secret_here"
    depends_on:
      - diun-tap

volumes:
  diuntap-state:
```

#### Option B: Standalone DiunTap (Existing DIUN)

If you already run DIUN in your homelab:

1. Add DiunTap to your `compose.yaml`:

```yaml
services:
  diun-tap:
    image: ghcr.io/patientone-io/diun-tap:latest
    container_name: diun-tap
    restart: unless-stopped
    cap_drop:
      - ALL
    group_add:
      - ${DOCKER_GID:-999} # must match `stat -c %g /var/run/docker.sock`
    ports:
      - "5000:5000"
    env_file:
      - .env
    environment:
      - TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
      - TELEGRAM_CHAT_ID=123456789
      - TELEGRAM_ALLOWED_USER_IDS=123456789 # optional, recommended for groups
      - DIUN_WEBHOOK_SECRET=your_secret_here # optional, recommended
      - GITHUB_TOKEN= # optional
      - LANGUAGE=en
      - DOCKER_STACKS_DIR=/srv/docker # Directory where your stacks are stored
      - DOCKER_GID=${DOCKER_GID:-999}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # 1:1 mount matching DOCKER_STACKS_DIR
      - diuntap-state:/data

volumes:
  diuntap-state:
```

2. Point DIUN to DiunTap:

**Via environment variables in DIUN's `compose.yaml`:**

```yaml
environment:
  - "DIUN_NOTIF_WEBHOOK_ENDPOINT=http://<diun-tap-host>:5000/webhook/diun"
  - "DIUN_NOTIF_WEBHOOK_METHOD=POST"
  - "DIUN_NOTIF_WEBHOOK_HEADERS=X-Diun-Secret: your_secret_here"
```

**Or in `diun.yml` (if using a configuration file):**

```yaml
notif:
  webhook:
    endpoint: "http://<diun-tap-host>:5000/webhook/diun"
    method: "POST"
    headers:
      Content-Type: "application/json"
      X-Diun-Secret: "your_secret_here"
```

*(If DIUN and DiunTap share the same Docker network, you can simply use `http://diun-tap:5000/webhook/diun`.)*

> **Migration note:** if `DIUN_WEBHOOK_SECRET` is empty, DiunTap logs a `WARNING` and accepts webhooks without auth (legacy mode), so existing installs keep working until you set the secret on both sides.

#### 3. Launch

Start your stack:

```bash
docker compose up -d
```

Verify service health (minimal public surface; details live behind the gated `/ready` endpoint):

```bash
curl http://localhost:5000/health
# {"status":"ok","app":"DiunTap","version":"1.1.0","telegram_configured":true,...}
```

> **Tip:** You can obtain a bot token from [@BotFather](https://t.me/BotFather) and your chat ID from [@userinfobot](https://t.me/userinfobot). For group use, list every allowed tapper in `TELEGRAM_ALLOWED_USER_IDS`.

---

### 🔑 Securing the DIUN webhook

1. Generate a long random secret and put it in DiunTap's `.env`:
   ```bash
   openssl rand -hex 24
   # .env: DIUN_WEBHOOK_SECRET=your_secret_here
   ```
2. Configure DIUN to send it (header preferred, query fallback):
   - Compose: `DIUN_NOTIF_WEBHOOK_HEADERS=X-Diun-Secret: your_secret_here`
   - or append `?token=your_secret_here` to the endpoint URL.
3. Restart both services. A wrong/missing secret is rejected with `401 Invalid webhook secret` and a warning in DiunTap logs.

### 👥 Telegram in groups

Set `TELEGRAM_ALLOWED_USER_IDS` to a comma-separated list of numeric user IDs (get yours from `@userinfobot`). DiunTap checks **only** the button tapper's `from_user.id` — the chat/group ID is never trusted for authorization. When the variable is empty, the single `TELEGRAM_CHAT_ID` is used as the allow-list (single-user mode).

---

### ⚙️ Configuration Reference

All settings can be configured via environment variables or a `.env` file:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from `@BotFather` | *(Required)* |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications; doubles as single-user allow-list fallback | *(Required)* |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs allowed to tap buttons (`from_user.id` only) | `""` (= fallback to `TELEGRAM_CHAT_ID`) |
| `DIUN_WEBHOOK_SECRET` | Shared secret for `/webhook/diun` (`X-Diun-Secret` header or `?token=`); empty = legacy mode + WARNING | `""` *(Optional, recommended)* |
| `GITHUB_TOKEN` | GitHub Personal Access Token (increases rate limit from 60 to 5,000 req/h) | `""` *(Optional)* |
| `LANGUAGE` | Interface language (`en` or `pl`) | `en` |
| `LOCAL_NODE_NAME` | Display name for the local Docker host | `Local` |
| `DOCKER_STACKS_DIR` | Directory where Docker Compose stacks are stored (mounted 1:1) | `/srv/docker` |
| `DOCKER_GID` | Host group id of `/var/run/docker.sock` (`stat -c %g …`); wired to `group_add` | `999` |
| `HEALTHCHECK_WAIT_SECONDS` | Observation window (timeout stays pending; healthchecks get at least 60s) | `60` |
| `DIUNTAP_STATE_DIR` | Persistent action and restore database directory | `/data` in Docker; `./data` locally |
| `SSH_TIMEOUT_SECONDS` | Timeout for a single SSH call | `30` |
| `DOCKER_TIMEOUT_SECONDS` | Timeout for `pull` / `up -d` | `180` |
| `WEBHOOK_PORT` | Port for the incoming DIUN webhook server | `5000` |
| `MAPPINGS_FILE` | Path to custom YAML or JSON mappings file | `""` *(Optional)* |
| `EXCLUDE_IMAGES` | CSV fnmatch patterns for images that never notify (e.g. `vaultwarden/server,*:nightly`) | `""` *(Optional)* |
| `PINNED_STACKS` | CSV local stack / remote service names to freeze (exact match, notifications skipped) | `""` *(Optional)* |
| `REMOTE_SSH_HOST` | SSH host alias for optional remote node updates | `""` *(Optional)* |
| `REMOTE_NODE_NAME` | Display name for the optional remote node | `Remote` |
| `REMOTE_CONFIGS_DIR` | Path to remote Quadlet/container config files | `/remote-configs` |

---

### 🗺️ Custom Image Mappings

DiunTap contains built-in mappings for dozens of common images (e.g. `linuxserver/jellyfin` -> `jellyfin/jellyfin`, `vaultwarden/server` -> `dani-garcia/vaultwarden`).

For custom or private images, create a `mappings.yaml` file:

```yaml
images:
  my-app: "organization/my-app-repo"
  ghcr.io/internal/dashboard: "internal/dashboard"
```

Mount the file into your container or set `MAPPINGS_FILE=/config/mappings.yaml`.

---

## 🇵🇱 Polski

<div align="center">
  <img src="assets/telegram-preview-pl.png" alt="DiunTap Interaktywny Przepływ Telegram" width="100%" style="max-width: 900px; border-radius: 10px;" />
</div>

---

### 💡 Czym jest DIUN i dlaczego DiunTap?

> [!NOTE]
> **Czym jest DIUN?**  
> [DIUN (Docker Image Update Notifier)](https://crazymax.dev/diun/) autorstwa [CrazyMax](https://github.com/crazy-max) to lekkie, popularne narzędzie open-source monitorujące rejestry kontenerów (Docker Hub, GHCR, Quay) i powiadamiające o nowych wersjach obrazów.

* **Problem z Watchtowerem:** Niekontrolowane, automatyczne aktualizacje potrafią wyłożyć krytyczne usługi lub bazy danych o 3 w nocy. Brak decyzji człowieka, brak changelogu, a powiadomienia przychodzą po fakcie.
* **Ograniczenie DIUN:** DIUN doskonale wykrywa nowe wersje obrazów, lecz z założenia wysyła jedynie suche, pasywne powiadomienia tekstowe. Nie potrafi wykonać aktualizacji ani pobrać release notes z GitHuba.
* **Rozwiązanie DiunTap:** **Human-in-the-Loop (Człowiek podejmuje decyzję).** Gdy DIUN wykryje nowy obraz, DiunTap pobiera changelog z GitHub API, wysyła czytelne powiadomienie na Telegram z przyciskami akcji i pozwala zaktualizować usługę jednym tapnięciem z telefonu. Jeśli coś pójdzie nie tak, funkcja 1-Click Rollback natychmiast przywraca działającą wersję.

---

### 🏗️ Jak to działa

```text
┌────────────────────────────────────────────────────────┐
│             DIUN (detektor nowych obrazów)             │
│  Skanuje rejestry (Docker Hub, GHCR, Quay, itd.)       │
└──────────────────────────┬─────────────────────────────┘
                           │ Webhook POST /webhook/diun
                           │ (opcjonalnie X-Diun-Secret)
                           ▼
┌────────────────────────────────────────────────────────┐
│                        DiunTap                         │
│                                                        │
│  1. Weryfikuje sekret webhooka (jeśli ustawiony)       │
│  2. Pobiera Release Notes z GitHub API                 │
│  3. Wykrywa stos Compose (etykiety lub katalog)        │
│  4. Formatuje HTML z <blockquote expandable>           │
│  5. Nasłuchuje kliknięć (Long Polling w LAN)           │
└──────────────────────────┬─────────────────────────────┘
                           │ Telegram Bot API
                           ▼
┌────────────────────────────────────────────────────────┐
│               Twój Telegram (telefon / PC)             │
│                                                        │
│  🚀 Aktualizacja: navidrome                            │
│  📋 Changelog: v0.55.0 (zwijany cytat)                 │
│  [ 🚀 Uaktualnij teraz ]  [ ❌ Pomiń ]                 │
│  [ 🌐 Otwórz Wydanie na GitHubie ]                     │
└──────────────────────────┬─────────────────────────────┘
                           │ Klik: [ 🚀 Uaktualnij teraz ]
                           ▼
┌────────────────────────────────────────────────────────┐
│                        DiunTap                         │
│                                                        │
│  1. Taguje obrazy jako punkty przywracania             │
│     (:diun-backup-<service>-<timestamp>)   │
│  2. Wykonuje: docker compose pull && up -d             │
│  3. Czeka na stabilizację (polling healthchecka)       │
│  4. Sukces: ✅ Gotowe! [ ⏪ Cofnij ]                    │
│  5. Błąd: ⚠️ 20 linijek logów + próba auto-rollbacku   │
│     + [ ⏪ Cofnij ]                                     │
└────────────────────────────────────────────────────────┘
```

**Układ kodu:** punktem wejścia jest `main.py` (aplikacja FastAPI, webhook, polling Telegrama, orkiestracja Dockera/SSH). `app.py` to cienki shim wstecznej kompatybilności (`from main import app`, więc `uvicorn app:app` działa dalej). Moduły pomocnicze: `config.py` (ustawienia), `models.py` (walidacja payloadu + nieprzezroczysty rejestr akcji przycisków), `security.py` (allow-listy, autoryzacja), `docker_local.py` (update/rollback/health), `remote_ssh.py` (bezpieczny SSH), `github_client.py`, `telegram_client.py`, `polling.py`, `dedup.py`, `errors.py`.

---

### ✨ Główne Funkcje

* 🚀 **Interaktywne Aktualizacje 1-Click (Human-in-the-Loop):** Decydujesz, kiedy zrestartować usługę bezpośrednio z poziomu komunikatora w telefonie.
* 📋 **Inteligentny Changelog z Rozwijanym Blokiem:** Parsowanie opisów wydań z GitHuba do natywnych bloków `<blockquote expandable>` Telegrama. Estetyczna typografia, pogrubione nagłówki, klikalne linki do PR i zwijana treść, dzięki czemu długie changelogi nie zaśmiecają czatu.
* 🎯 **Podwójna Detekcja Stosów:** Automatyczne odczytywanie nazwy projektu i ścieżki roboczej z etykiet działających kontenerów (`com.docker.compose.project`) z niezawodnym fallbackiem do skanowania katalogów na dysku.
* 🛡️ **Deduplikacja Powiadomień:** Ograniczony pamięciowo `TTLCache` zapobiega wielokrotnym alertom o tym samym tagu — bez wycieku pamięci w długim czasie działania.
* 📌 **Przypinanie / Zamrażanie:** `EXCLUDE_IMAGES` (wzorce z wildcardami) i `PINNED_STACKS` (dokładne nazwy) po cichu pomijają powiadomienia dla kontenerów, które mają zostać na danej wersji.
* 🔒 **100% Lokalnie i Zero Trust (Long Polling):** Nasłuchiwanie przycisków przez wbudowaną pętlę Long Polling - brak konieczności wystawiania publicznych portów, domen czy tuneli Cloudflare.
* 🔑 **Opcjonalna Autoryzacja Webhooka:** Ustaw `DIUN_WEBHOOK_SECRET`, a DIUN podpisze każde wywołanie nagłówkiem `X-Diun-Secret` (lub `?token=`). Zły sekret → `401`. Brak zmiennej → tryb legacy z wyraźnym `WARNING` w logach (płynna migracja).
* 👥 **Bezpieczna Autoryzacja w Grupach:** Weryfikowane jest **wyłącznie** `from_user.id` klikającego względem `TELEGRAM_ALLOWED_USER_IDS` (fallback do `TELEGRAM_CHAT_ID`). Bota można bezpiecznie używać w grupach — obcy klikający dostaje `Brak uprawnień`.
* 🩺 **Automatyczny Healthcheck i Diagnostyka:** Stan kontenerów jest odpytywany aż do stabilizacji (lub timeoutu). Jeśli usługa zgłosi błąd, bot przechwytuje ostatnie 20 linijek logów, wysyła je na czat i próbuje automatycznego rollbacku.
* ⏪ **1-Click Rollback:** Przed pobraniem identyfikator każdego aktualizowanego obrazu dostaje tag backupu (lokalnie z timestampem i skrótem referencji; retencja: 3 najnowsze na referencję, obsługa stosów multi-service). Rollback stawia kontenery przez `up -d --pull never`, więc uszkodzony `:latest` nigdy nie zostanie przypadkowo dociągnięty.
* 🧩 **Modularna, Utwardzona Baza Kodu:** Surowe allow-listy danych wejściowych (brak wstrzyknięć do shella po SSH), wymuszone timeouty podprocesów, nieblokujące skanowanie plików, serializacja aktualizacji na stos (bezpieczny podwójny klik).
* 🌐 **Wsparcie Multi-Node (Opcjonalnie):** Domyślnie lokalny Docker Compose, z opcją obsługi zdalnych węzłów po SSH (np. Podman Quadlets czy zdalny serwer VPS). Komendy zdalne działają z `StrictHostKeyChecking=yes`.
* 🌍 **Wielojęzyczność:** Pełne wsparcie dla języka polskiego (`LANGUAGE=pl`) oraz angielskiego (`LANGUAGE=en`).
* 📁 **Rozszerzalne Mapowania Obrazów:** Baza popularnych serwisów z możliwością dodania własnych definicji przez `mappings.yaml`.

---

### Zasady aktualizacji i przywracania

DiunTap aktualizuje tylko usługi, których pełna nazwa repozytorium obrazu i skonfigurowany tag pasują do powiadomienia. Nie zmienia plików Compose ani Quadletów. Przejście z przypiętego `v1` na `v2` wymaga wcześniejszej zmiany konfiguracji. Usługi przypięte do digestu są pomijane. Pozostałe usługi Compose nie są pobierane ani odtwarzane.

Healthcheck wymaga dwóch kolejnych poprawnych odczytów; `starting` oznacza oczekiwanie. Domyślny czas obserwacji to 60 sekund. Kontenery z healthcheckiem dostają co najmniej 60 sekund, także przy starym `HEALTHCHECK_WAIT_SECONDS=6`. Timeout lub brak odczytu oznacza wynik nierozstrzygnięty i komunikat o oczekiwaniu, bez automatycznego rollbacku. Dopiero potwierdzona awaria (`unhealthy`, `exited`, `dead`, `restarting` lub zdalna jednostka w stanie `failed`) albo błąd polecenia uruchomienia powoduje próbę automatycznego rollbacku. Rollback przywraca obrazy kontenerów, ale nie dane aplikacji ani migracje bazy danych.

Akcje Telegrama i ostatni punkt przywracania dla każdego celu są zapisywane w SQLite. Zamontuj `diuntap-state:/data` jak w przykładach poniżej; poza Dockerem ustaw `DIUNTAP_STATE_DIR` (domyślnie `./data`). Nowe przyciski przetrwają restart przez maksymalnie siedem dni, z limitem 4096 akcji. Przycisków wysłanych przez starszą wersję nie da się odzyskać. Rollback wymaga punktu przywracania utworzonego przez tę wersję i przywraca ostatnią zapisaną aktualizację danego celu.

W zdalnych Quadletach `ContainerName=` musi odpowiadać nazwie usługi bez rozszerzenia. Obsługiwane są domyślna polityka `missing`, jawne `Pull=missing` i `Pull=never`, bez wymuszania zmian istniejących jednostek. DiunTap sam pobiera obraz przed restartem. Załadowana jednostka musi wskazywać to samo repozytorium i tag (prefiksy Docker Huba są normalizowane); `always` i `newer` są odrzucane, bo mogłyby nadpisać przywrócony lokalny obraz. Zobacz [dokumentację Quadletów Podmana](https://docs.podman.io/en/stable/markdown/podman-systemd.unit.5.html).

### 🚀 Szybki Start

Uruchom DiunTap za pomocą Docker Compose w kilka sekund. **Kontener działa jako non-root (`USER diuntap`)** i potrzebuje grupy gniazda Dockera z hosta — patrz krok 0.

#### 0. Grupa gniazda Dockera (raz na host)

```bash
stat -c %g /var/run/docker.sock   # np. 999 na Debianie, 998 gdzie indziej
echo "DOCKER_GID=999" >> .env     # wpisz SWÓJ numer
```

Zły numer = czytelny błąd `Permission denied on /var/run/docker.sock` w logach zamiast dostępu do Dockera.

#### Opcja A: Wszystko w jednym (DiunTap + DIUN w jednym compose.yaml)

Jeśli nie masz jeszcze uruchomionego kontenera DIUN, uruchom kompletny stos w jednym pliku `compose.yaml`:

```yaml
services:
  diun-tap:
    image: ghcr.io/patientone-io/diun-tap:latest
    container_name: diun-tap
    restart: unless-stopped
    cap_drop:
      - ALL
    group_add:
      - ${DOCKER_GID:-999} # musi zgadzać się z `stat -c %g /var/run/docker.sock`
    ports:
      - "5000:5000"
    env_file:
      - .env
    environment:
      - TELEGRAM_BOT_TOKEN=twoj_token_z_botfather
      - TELEGRAM_CHAT_ID=twoj_chat_id
      - TELEGRAM_ALLOWED_USER_IDS=twoj_chat_id # opcjonalnie, zalecane przy grupach
      - DIUN_WEBHOOK_SECRET=your_secret_here # opcjonalnie, zalecane
      - GITHUB_TOKEN= # opcjonalnie (zwiększa limit zapytań API z 60 do 5000/h)
      - LANGUAGE=pl
      - DOCKER_STACKS_DIR=/srv/docker # Katalog ze stosami Docker Compose
      - DOCKER_GID=${DOCKER_GID:-999}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # Montowanie 1:1 zgodne z DOCKER_STACKS_DIR
      - diuntap-state:/data

  diun:
    image: crazymax/diun:latest
    container_name: diun
    command: serve
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./diun-data:/data
    environment:
      - "TZ=Europe/Warsaw"
      - "DIUN_WATCH_SCHEDULE=0 */6 * * *"
      - "DIUN_PROVIDERS_DOCKER=true"
      - "DIUN_PROVIDERS_DOCKER_WATCHBYDEFAULT=true"
      - "DIUN_NOTIF_WEBHOOK_ENDPOINT=http://diun-tap:5000/webhook/diun"
      - "DIUN_NOTIF_WEBHOOK_METHOD=POST"
      - "DIUN_NOTIF_WEBHOOK_HEADERS=X-Diun-Secret: your_secret_here"
    depends_on:
      - diun-tap

volumes:
  diuntap-state:
```

#### Opcja B: Samodzielny DiunTap (podłączenie do istniejącego DIUN)

Jeśli masz już działający kontener DIUN:

1. Dodaj DiunTap do swojego pliku `compose.yaml`:

```yaml
services:
  diun-tap:
    image: ghcr.io/patientone-io/diun-tap:latest
    container_name: diun-tap
    restart: unless-stopped
    cap_drop:
      - ALL
    group_add:
      - ${DOCKER_GID:-999} # musi zgadzać się z `stat -c %g /var/run/docker.sock`
    ports:
      - "5000:5000"
    env_file:
      - .env
    environment:
      - TELEGRAM_BOT_TOKEN=twoj_token_z_botfather
      - TELEGRAM_CHAT_ID=twoj_chat_id
      - TELEGRAM_ALLOWED_USER_IDS=twoj_chat_id # opcjonalnie, zalecane przy grupach
      - DIUN_WEBHOOK_SECRET=your_secret_here # opcjonalnie, zalecane
      - GITHUB_TOKEN= # opcjonalnie
      - LANGUAGE=pl
      - DOCKER_STACKS_DIR=/srv/docker # Katalog ze stosami Docker Compose
      - DOCKER_GID=${DOCKER_GID:-999}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # Montowanie 1:1 zgodne z DOCKER_STACKS_DIR
      - diuntap-state:/data

volumes:
  diuntap-state:
```

2. Skieruj DIUN na DiunTap:

**Poprzez zmienne środowiskowe w `compose.yaml` kontenera DIUN:**

```yaml
environment:
  - "DIUN_NOTIF_WEBHOOK_ENDPOINT=http://<adres-hosta-diun-tap>:5000/webhook/diun"
  - "DIUN_NOTIF_WEBHOOK_METHOD=POST"
  - "DIUN_NOTIF_WEBHOOK_HEADERS=X-Diun-Secret: your_secret_here"
```

**Lub w pliku konfiguracyjnym `diun.yml` (jeśli z niego korzystasz):**

```yaml
notif:
  webhook:
    endpoint: "http://<adres-hosta-diun-tap>:5000/webhook/diun"
    method: "POST"
    headers:
      Content-Type: "application/json"
      X-Diun-Secret: "your_secret_here"
```

*(Jeśli DIUN i DiunTap pracują w tej samej sieci Dockera, wystarczy podać adres wewnętrzny: `http://diun-tap:5000/webhook/diun`.)*

> **Migracja:** gdy `DIUN_WEBHOOK_SECRET` jest puste, DiunTap loguje `WARNING` i przyjmuje webhooki bez autoryzacji (tryb legacy), więc istniejące instalacje działają do czasu ustawienia sekretu po obu stronach.

#### 3. Uruchomienie

Uruchom kontener:

```bash
docker compose up -d
```

Sprawdź stan usługi (minimalna publiczna odpowiedź; szczegóły w bramkowanym endpoincie `/ready`):

```bash
curl http://localhost:5000/health
# {"status":"ok","app":"DiunTap","version":"1.1.0","telegram_configured":true,...}
```

> **Wskazówka:** Token bota uzyskasz z [@BotFather](https://t.me/BotFather), a swój identyfikator z [@userinfobot](https://t.me/userinfobot). Do użycia w grupach wpisz wszystkich uprawnionych w `TELEGRAM_ALLOWED_USER_IDS`.

---

### 🔑 Zabezpieczenie webhooka DIUN

1. Wygeneruj długi losowy sekret i wpisz go do `.env` DiunTap:
   ```bash
   openssl rand -hex 24
   # .env: DIUN_WEBHOOK_SECRET=your_secret_here
   ```
2. Skonfiguruj DIUN, by go wysyłał (nagłówek zalecany, query jako fallback):
   - Compose: `DIUN_NOTIF_WEBHOOK_HEADERS=X-Diun-Secret: your_secret_here`
   - albo dopisz `?token=your_secret_here` do URL endpointu.
3. Zrestartuj obie usługi. Zły/brakujący sekret to `401 Invalid webhook secret` plus ostrzeżenie w logach DiunTap.

### 👥 Telegram w grupach

Ustaw `TELEGRAM_ALLOWED_USER_IDS` jako listę numerycznych ID użytkowników oddzielonych przecinkami (swój poznasz przez `@userinfobot`). DiunTap sprawdza **wyłącznie** `from_user.id` klikającego — ID czatu/grupy nigdy nie jest podstawą autoryzacji. Gdy zmienna jest pusta, allow-listą jest pojedynczy `TELEGRAM_CHAT_ID` (tryb jednoosobowy).

---

### ⚙️ Tabela konfiguracji

Wszystkie ustawienia przez zmienne środowiskowe lub plik `.env`:

| Zmienna | Opis | Domyślnie |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Token bota z `@BotFather` | *(Wymagany)* |
| `TELEGRAM_CHAT_ID` | ID czatu do powiadomień; fallback allow-listy jednoosobowej | *(Wymagany)* |
| `TELEGRAM_ALLOWED_USER_IDS` | ID użytkowników mogących klikać (tylko `from_user.id`) | `""` (= fallback do `TELEGRAM_CHAT_ID`) |
| `DIUN_WEBHOOK_SECRET` | Współdzielony sekret `/webhook/diun` (nagłówek `X-Diun-Secret` lub `?token=`); pusty = tryb legacy + WARNING | `""` *(opcjonalny, zalecany)* |
| `GITHUB_TOKEN` | Token GitHub (limit API z 60 do 5000 req/h) | `""` *(opcjonalny)* |
| `LANGUAGE` | Język interfejsu (`en` lub `pl`) | `en` |
| `LOCAL_NODE_NAME` | Nazwa wyświetlana lokalnego hosta Docker | `Local` |
| `DOCKER_STACKS_DIR` | Katalog ze stosami Compose (montowany 1:1) | `/srv/docker` |
| `DOCKER_GID` | GID gniazda `/var/run/docker.sock` z hosta (`stat -c %g …`); podpinany przez `group_add` | `999` |
| `HEALTHCHECK_WAIT_SECONDS` | Czas obserwacji (timeout pozostaje nierozstrzygnięty; healthcheck ma minimum 60s) | `60` |
| `DIUNTAP_STATE_DIR` | Katalog trwałej bazy akcji i punktów przywracania | `/data` w Dockerze; `./data` lokalnie |
| `SSH_TIMEOUT_SECONDS` | Timeout pojedynczego wywołania SSH | `30` |
| `DOCKER_TIMEOUT_SECONDS` | Timeout `pull` / `up -d` | `180` |
| `WEBHOOK_PORT` | Port serwera webhooków DIUN | `5000` |
| `MAPPINGS_FILE` | Ścieżka do własnego pliku mapowań YAML/JSON | `""` *(opcjonalnie)* |
| `EXCLUDE_IMAGES` | Wzorce fnmatch obrazów CSV, które nigdy nie powiadamiają (np. `vaultwarden/server,*:nightly`) | `""` *(opcjonalnie)* |
| `PINNED_STACKS` | Nazwy CSV lokalnych stosów / zdalnych usług do zamrożenia (dokładne dopasowanie, powiadomienia pomijane) | `""` *(opcjonalnie)* |
| `REMOTE_SSH_HOST` | Alias SSH opcjonalnego zdalnego węzła | `""` *(opcjonalnie)* |
| `REMOTE_NODE_NAME` | Nazwa wyświetlana zdalnego węzła | `Remote` |
| `REMOTE_CONFIGS_DIR` | Katalog zdalnych plików Quadlet/kontenerów | `/remote-configs` |

---

### 🗺️ Własne Mapowania Obrazów

DiunTap ma wbudowane mapowania dla dziesiątek popularnych obrazów (np. `linuxserver/jellyfin` -> `jellyfin/jellyfin`, `vaultwarden/server` -> `dani-garcia/vaultwarden`).

Dla własnych lub prywatnych obrazów utwórz plik `mappings.yaml`:

```yaml
images:
  my-app: "organization/my-app-repo"
  ghcr.io/internal/dashboard: "internal/dashboard"
```

Zamontuj go w kontenerze lub ustaw `MAPPINGS_FILE=/config/mappings.yaml`.

---

## 🔒 Security & Privacy / Bezpieczeństwo i prywatność

* **Non-root by default:** the container runs as `USER diuntap` (uid 1000) with `cap_drop: ALL`. Docker socket access is granted via the host-matching group (`group_add: ${DOCKER_GID}`) instead of running as root.
* **Zero Public Attack Surface:** Telegram communication uses Long Polling (`getUpdates`). You do not need to open any firewall ports or expose webhooks to the internet.
* **Access Control:** DiunTap verifies **only** the button tapper's `from_user.id` against `TELEGRAM_ALLOWED_USER_IDS` (falling back to `TELEGRAM_CHAT_ID`). Actions triggered by unauthorized users are rejected immediately. The DIUN webhook itself is optionally signed with `DIUN_WEBHOOK_SECRET` (`401` on mismatch).
* **Hardened execution:** strict input allow-lists, `shlex`-quoted SSH atoms, enforced timeouts on every subprocess/SSH call, symlink-aware stack resolution, serialized per-stack updates.
* **Local-First & Zero Telemetry:** No analytics, no tracking, and no external proxies. All communication happens strictly between DIUN, your local Docker daemon, GitHub API, and Telegram. Bot tokens never appear in logs; `/health` exposes no hostnames or paths (details live behind the gated `/ready` endpoint).

---

## 🛠️ Building from Source / Budowanie ze źródeł

For contributors or developers who prefer to build the Docker image locally from source:  
*Dla programistów i kontrybutorów chcących zbudować obraz lokalnie ze źródeł:*

```bash
git clone https://github.com/patientone-io/diun-tap.git
cd diun-tap
docker build --build-arg DOCKER_GID=$(stat -c %g /var/run/docker.sock) -t diun-tap .
docker compose up -d
```

> The `--build-arg DOCKER_GID=...` bakes the host docker-socket group into the image so the non-root `diuntap` user keeps socket access. It must match the `DOCKER_GID` / `group_add` value used at runtime.  
> *Parametr `--build-arg DOCKER_GID=...` wbudowuje grupę gniazda Dockera z hosta w obraz, dzięki czemu non-root `diuntap` zachowuje dostęp do socketa. Musi zgadzać się z wartością `DOCKER_GID` / `group_add` użytą w runtime.*

---

## 👤 Author & Contact / Autor i kontakt

* **Author:** patientone
* **Contact:** [contact@patientone.uk](mailto:contact@patientone.uk)
* **GitHub:** [https://github.com/patientone-io](https://github.com/patientone-io)
* **Website:** [https://patientone.uk](https://patientone.uk)

---

## 📄 License / Licencja

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.  
*Projekt udostępniany na zasadach licencji MIT - szczegóły w pliku [LICENSE](LICENSE).*
