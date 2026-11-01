#!/usr/bin/env bash
# Installs or updates the bot on an Ubuntu server. Run with sudo.
# A failing test stops the script before the live copy is touched.
set -euo pipefail

APP_USER=pokerbot
APP_HOME=/home/$APP_USER
APP_DIR=$APP_HOME/pokerBot
VENV=$APP_HOME/venv
KEY_FILE=$APP_HOME/.openpoker_key
SRC=$(cd "$(dirname "$0")/.." && pwd)

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

if [ ! -d "$VENV" ]; then
    sudo -u "$APP_USER" python3 -m venv "$VENV"
fi
sudo -u "$APP_USER" "$VENV/bin/pip" install --quiet --upgrade pip
# The upload sits in the login user's home, which the bot user can't read.
install -m 644 "$SRC/requirements.txt" /tmp/pokerbot-requirements.txt
sudo -u "$APP_USER" "$VENV/bin/pip" install --quiet -r /tmp/pokerbot-requirements.txt
rm -f /tmp/pokerbot-requirements.txt

"$VENV/bin/python" -m pytest "$SRC/tests" -q -p no:cacheprovider

mkdir -p "$APP_DIR/logs"
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name logs -exec rm -rf {} +
cp -r "$SRC"/. "$APP_DIR"/
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

if [ ! -f "$KEY_FILE" ]; then
    install -o "$APP_USER" -g "$APP_USER" -m 600 /dev/null "$KEY_FILE"
fi

cp "$SRC/deploy/pokerbot.service" /etc/systemd/system/pokerbot.service
systemctl daemon-reload
systemctl enable --quiet pokerbot

if [ -s "$KEY_FILE" ]; then
    systemctl restart pokerbot
    echo "Bot restarted with the new code."
else
    echo "Installed. Add your API key with: sudo nano $KEY_FILE"
    echo "Then start the bot with: sudo systemctl start pokerbot"
fi
