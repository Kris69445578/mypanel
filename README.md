# MyPanel

A lightweight Pterodactyl-style panel: create Docker-isolated servers, start/stop them, use a live web terminal, edit files, manage users. Python (FastAPI) + Docker + Nginx. Built for Ubuntu 22.04.

## One-command install

```bash
curl -sSL https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/mypanel/main/install.sh | sudo REPO_URL=https://github.com/YOUR_GITHUB_USERNAME/mypanel.git bash
```

With HTTPS (point your domain's A record at the server first):

```bash
curl -sSL https://raw.githubusercontent.com/Kris69445578/mypanel/main/install.sh | sudo DOMAIN=panel.jahim.dpdns.org EMAIL=wamitiantony297@gmail.com REPO_URL=https://github.com/Kris69445578/mypanel.git bash
```

The installer sets up Docker, Nginx, a Python venv, a systemd service and an admin account, then prints the URL and a random admin password. Re-running it updates the panel and keeps your data.

## Commands

`mypanel status | logs | restart | update | reset-password <user> <pass> | uninstall`

## Eggs (Pterodactyl style)

Drop PTDL_v2 egg JSON files into `panel/eggs/` (file `egg-python-bot.json` becomes egg id `python-bot`) and restart the panel.
Included: **Python Bot (Git Deploy)** and **Node.js Bot (Git Deploy)**.

Creating a server from an egg works like Pterodactyl:

1. **Install** - the egg's install script runs once in a throw-away container (the egg's install image, your server files mounted at `/mnt/server`). The output streams into the Console tab. If it fails the server shows `install failed`; fix the variables and press **Reinstall**.
2. **Start** - a fresh container is created from the chosen docker image (e.g. `ghcr.io/parkervcp/yolks:python_3.12`), the egg variables plus `STARTUP` are passed as environment, files are mounted at `/home/container`. The status is `starting` until the egg's "done" string (for these eggs: `Bot started`) shows up in the console, or 30 s pass (`PANEL_STARTUP_GRACE`).
3. **Stop** - uses the egg's stop setting (`^C` = SIGINT), then kills after 20 s (`PANEL_STOP_TIMEOUT`).

Edit variables later in the server's **Startup** tab; they apply on the next start. Old template-based servers keep working unchanged.

Run the tests (no Docker needed): `pip install -r requirements.txt httpx pytest && cd tests && python -m pytest`

## Security notes

- Anyone who controls the panel controls Docker, which is root-equivalent. Use a strong password and HTTPS.
- Containers drop all capabilities except a minimal set, use `no-new-privileges`, and have CPU/RAM limits.
- Only admins create servers; normal users see and control only servers assigned to them.
- Login attempts are rate limited. Keep the server patched.
