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
                           ▼
┌────────────────────────────────────────────────────────┐
│                        DiunTap                         │
│                                                        │
│  1. Fetches Release Notes from GitHub API              │
│  2. Identifies Compose stack via labels or directory   │
│  3. Formats Telegram HTML with <blockquote expandable> │
│  4. Listens for button taps (LAN Long Polling)         │
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
│  1. Tags current image as restore point (:diun-backup) │
│  2. Executes: docker compose pull && up -d             │
│  3. Waits for healthcheck stabilization                │
│  4. If healthy: ✅ Success! [ ⏪ Rollback ]            │
│  5. If failed:  ⚠️ Shows last 20 log lines + [Rollback]│
└────────────────────────────────────────────────────────┘
```

---

### ✨ Features

* 🚀 **Human-in-the-Loop 1-Click Updates:** Decide exactly when to update your containers with a single tap from your phone or desktop.
* 📋 **Smart GitHub Changelogs with Expandable Quotes:** Formats GitHub Releases markdown into Telegram-native `<blockquote expandable>` blocks. Clean typography, bold headers, clickable PR links, and a collapsible quote so long changelogs never clutter your chat.
* 🎯 **Dual Stack Discovery:** Automatically identifies the target Compose stack and working directory directly from Docker container labels (`com.docker.compose.project`), with static filesystem directory scan as a reliable fallback.
* 🛡️ **Notification Deduplication:** Built-in debounce window prevents duplicate alerts when DIUN triggers concurrent webhooks for the same tag.
* 🔒 **100% LAN & Zero-Trust Friendly (Long Polling):** Receives Telegram button clicks using built-in Long Polling. **Zero open ports, zero port-forwarding, and no reverse proxies or Cloudflare tunnels required.**
* 🩺 **Post-Update Healthcheck & Auto-Diagnostics:** Waits for container stabilization after deployment. If a container exits or reports `unhealthy`, DiunTap captures the last 20 lines of logs and sends them directly to Telegram.
* ⏪ **1-Click Rollback:** Automatically tags the running image as a backup (`:diun-backup-...`) before pulling. If an update introduces a breaking bug, tap `[ ⏪ Rollback ]` to instantly restore the previous working image.
* 🌐 **Multi-Node Ready (Optional):** Supports local Docker Compose stacks out-of-the-box, with built-in optional support for remote nodes via SSH (e.g. Podman Quadlets or remote VPS).
* 🌍 **Localization:** Built-in support for English (`LANGUAGE=en`) and Polish (`LANGUAGE=pl`).
* 📁 **Extensible Image Mappings:** Includes built-in mappings for dozens of popular homelab services, with support for custom overrides via `mappings.yaml`.

---

### 🚀 Quickstart

Deploy DiunTap using Docker Compose in seconds:

#### Option A: All-in-One Compose (DiunTap + DIUN together)

If you do not have DIUN running yet, deploy both services together in a single `compose.yaml`:

```yaml
services:
  diun-tap:
    image: ghcr.io/patientone-io/diun-tap:latest
    container_name: diun-tap
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
      - TELEGRAM_CHAT_ID=123456789
      - GITHUB_TOKEN= # optional (increases rate limit from 60 to 5,000 req/h)
      - LANGUAGE=en
      - DOCKER_STACKS_DIR=/srv/docker # Directory where your stacks are stored
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # 1:1 mount matching DOCKER_STACKS_DIR

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
    depends_on:
      - diun-tap
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
    ports:
      - "5000:5000"
    environment:
      - TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
      - TELEGRAM_CHAT_ID=123456789
      - GITHUB_TOKEN= # optional
      - LANGUAGE=en
      - DOCKER_STACKS_DIR=/srv/docker # Directory where your stacks are stored
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # 1:1 mount matching DOCKER_STACKS_DIR
```

2. Point DIUN to DiunTap:

**Via environment variables in DIUN's `compose.yaml`:**

```yaml
environment:
  - "DIUN_NOTIF_WEBHOOK_ENDPOINT=http://<diun-tap-host>:5000/webhook/diun"
  - "DIUN_NOTIF_WEBHOOK_METHOD=POST"
```

**Or in `diun.yml` (if using a configuration file):**

```yaml
notif:
  webhook:
    endpoint: "http://<diun-tap-host>:5000/webhook/diun"
    method: "POST"
    headers:
      Content-Type: "application/json"
```

*(If DIUN and DiunTap share the same Docker network, you can simply use `http://diun-tap:5000/webhook/diun`.)*

#### 3. Launch

Start your stack:

```bash
docker compose up -d
```

Verify service health:

```bash
curl http://localhost:5000/health
# {"status":"ok","app":"DiunTap","version":"1.0.0",...}
```

> **Tip:** You can obtain a bot token from [@BotFather](https://t.me/BotFather) and your chat ID from [@userinfobot](https://t.me/userinfobot).


---

### ⚙️ Configuration Reference

All settings can be configured via environment variables or a `.env` file:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from `@BotFather` | *(Required)* |
| `TELEGRAM_CHAT_ID` | Telegram chat ID authorized to receive notifications and click buttons | *(Required)* |
| `GITHUB_TOKEN` | GitHub Personal Access Token (increases rate limit from 60 to 5,000 req/h) | `""` *(Optional)* |
| `LANGUAGE` | Interface language (`en` or `pl`) | `en` |
| `LOCAL_NODE_NAME` | Display name for the local Docker host | `Local` |
| `DOCKER_STACKS_DIR` | Directory where Docker Compose stacks are stored (mounted 1:1) | `/srv/docker` |
| `HEALTHCHECK_WAIT_SECONDS` | Seconds to wait before checking container health after deployment | `6` |
| `WEBHOOK_PORT` | Port for the incoming DIUN webhook server | `5000` |
| `MAPPINGS_FILE` | Path to custom YAML or JSON mappings file | `""` *(Optional)* |
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

### ✨ Główne Funkcje

* 🚀 **Interaktywne Aktualizacje 1-Click (Human-in-the-Loop):** Decydujesz, kiedy zrestartować usługę bezpośrednio z poziomu komunikatora w telefonie.
* 📋 **Inteligentny Changelog z Rozwijanym Blokiem:** Parsowanie opisów wydań z GitHuba do natywnych bloków `<blockquote expandable>` Telegrama. Estetyczna typografia, pogrubione nagłówki, klikalne linki do PR i zwijana treść, dzięki czemu długie changelogi nie zaśmiecają czatu.
* 🎯 **Podwójna Detekcja Stosów:** Automatyczne odczytywanie nazwy projektu i ścieżki roboczej z etykiet działających kontenerów (`com.docker.compose.project`) z niezawodnym fallbackiem do skanowania katalogów na dysku.
* 🛡️ **Deduplikacja Powiadomień:** Wbudowane okno debounce zapobiega wielokrotnym alertom o tym samym tagu w krótkim odstępie czasu.
* 🔒 **100% Lokalnie i Zero Trust (Long Polling):** Nasłuchiwanie przycisków przez wbudowaną pętlę Long Polling - brak konieczności wystawiania publicznych portów, domen czy tuneli Cloudflare.
* 🩺 **Automatyczny Healthcheck i Diagnostyka:** Sprawdzenie stanu kontenerów po restarcie. Jeśli usługa zgłosi błąd, bot przechwytuje ostatnie 20 linijek logów i przesyła je na czacie.
* ⏪ **1-Click Rollback:** Oznaczanie poprzedniego obrazu tagiem przywracania (`:diun-backup-...`) przed aktualizacją. W razie problemu jedno kliknięcie przywraca poprzedni stan.
* 🌐 **Wsparcie Multi-Node (Opcjonalnie):** Domyślnie lokalny Docker Compose, z opcją obsługi zdalnych węzłów po SSH (np. Podman Quadlets czy zdalny serwer VPS).
* 🌍 **Wielojęzyczność:** Pełne wsparcie dla języka polskiego (`LANGUAGE=pl`) oraz angielskiego (`LANGUAGE=en`).
* 📁 **Rozszerzalne Mapowania Obrazów:** Baza popularnych serwisów z możliwością dodania własnych definicji przez `mappings.yaml`.

---

### 🚀 Szybki Start

Uruchom DiunTap za pomocą Docker Compose w kilka sekund:

#### Opcja A: Wszystko w jednym (DiunTap + DIUN w jednym compose.yaml)

Jeśli nie masz jeszcze uruchomionego kontenera DIUN, uruchom kompletny stos w jednym pliku `compose.yaml`:

```yaml
services:
  diun-tap:
    image: ghcr.io/patientone-io/diun-tap:latest
    container_name: diun-tap
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - TELEGRAM_BOT_TOKEN=twoj_token_z_botfather
      - TELEGRAM_CHAT_ID=twoj_chat_id
      - GITHUB_TOKEN= # opcjonalnie (zwiększa limit zapytań API z 60 do 5000/h)
      - LANGUAGE=pl
      - DOCKER_STACKS_DIR=/srv/docker # Katalog ze stosami Docker Compose
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # Montowanie 1:1 zgodne z DOCKER_STACKS_DIR

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
    depends_on:
      - diun-tap
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
    ports:
      - "5000:5000"
    environment:
      - TELEGRAM_BOT_TOKEN=twoj_token_z_botfather
      - TELEGRAM_CHAT_ID=twoj_chat_id
      - GITHUB_TOKEN= # opcjonalnie
      - LANGUAGE=pl
      - DOCKER_STACKS_DIR=/srv/docker # Katalog ze stosami Docker Compose
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/docker:/srv/docker # Montowanie 1:1 zgodne z DOCKER_STACKS_DIR
```

2. Skieruj DIUN na DiunTap:

**Poprzez zmienne środowiskowe w `compose.yaml` kontenera DIUN:**

```yaml
environment:
  - "DIUN_NOTIF_WEBHOOK_ENDPOINT=http://<adres-hosta-diun-tap>:5000/webhook/diun"
  - "DIUN_NOTIF_WEBHOOK_METHOD=POST"
```

**Lub w pliku konfiguracyjnym `diun.yml` (jeśli z niego korzystasz):**

```yaml
notif:
  webhook:
    endpoint: "http://<adres-hosta-diun-tap>:5000/webhook/diun"
    method: "POST"
    headers:
      Content-Type: "application/json"
```

*(Jeśli DIUN i DiunTap pracują w tej samej sieci Dockera, wystarczy podać adres wewnętrzny: `http://diun-tap:5000/webhook/diun`.)*

#### 3. Uruchomienie

Uruchom kontener:

```bash
docker compose up -d
```

Sprawdź stan usługi:

```bash
curl http://localhost:5000/health
# {"status":"ok","app":"DiunTap","version":"1.0.0",...}
```

> **Wskazówka:** Token bota uzyskasz z [@BotFather](https://t.me/BotFather), a swój identyfikator z [@userinfobot](https://t.me/userinfobot).


---

## 🔒 Security & Privacy / Bezpieczeństwo i prywatność

* **Zero Public Attack Surface:** Telegram communication uses Long Polling (`getUpdates`). You do not need to open any firewall ports or expose webhooks to the internet.
* **Access Control:** DiunTap verifies the sender `user_id` and `chat_id` on every button callback. Actions triggered by unauthorized users are rejected immediately.
* **Local-First & Zero Telemetry:** No analytics, no tracking, and no external proxies. All communication happens strictly between DIUN, your local Docker daemon, GitHub API, and Telegram.

## 🛠️ Building from Source / Budowanie ze źródeł

For contributors or developers who prefer to build the Docker image locally from source:  
*Dla programistów i kontrybutorów chcących zbudować obraz lokalnie ze źródeł:*

```bash
git clone https://github.com/patientone-io/diun-tap.git
cd diun-tap
docker compose build
```

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
