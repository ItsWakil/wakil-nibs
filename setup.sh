#!/usr/bin/env bash
#
# setup.sh: venv, password, secret key, optional systemd service and nginx config.
# Safe to re-run. Usage: bash setup.sh

set -e

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RESET='\033[0m'

step() { echo -e "\n${BOLD}${GREEN}==>${RESET} ${BOLD}$1${RESET}"; }
note() { echo -e "${YELLOW}$1${RESET}"; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo -e "${BOLD}Wakil Nibs -- setup${RESET}"
echo "Project folder: $PROJECT_DIR"

# Check Python is available
step "Checking for Python 3"
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found. Install it first:"
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip"
    echo "  Arch:           sudo pacman -S python"
    exit 1
fi
echo "Found $(python3 --version)"

# Create the virtual environment
step "Setting up the Python virtual environment"
if [ -d "venv" ]; then
    echo "venv/ already exists, skipping creation."
else
    if ! python3 -m venv venv 2>/tmp/venv_error.log; then
        cat /tmp/venv_error.log
        echo
        note "venv creation failed. On Debian/Ubuntu you likely need:"
        echo "  sudo apt install python3-venv"
        exit 1
    fi
    echo "Created venv/"
fi

step "Installing dependencies"
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt
echo "Installed Flask, gunicorn, and friends into venv/"

# Choose a login password
step "Choose the password for adding/editing pens and inks"
note "Anyone can view the site; only people with this password can make changes."
while true; do
    read -rsp "Password: " PENTRACKER_PASSWORD
    echo
    read -rsp "Confirm password: " PASSWORD_CONFIRM
    echo
    if [ "$PENTRACKER_PASSWORD" == "$PASSWORD_CONFIRM" ] && [ -n "$PENTRACKER_PASSWORD" ]; then
        break
    fi
    echo "Passwords didn't match (or were empty) -- try again."
done

# Generate a random secret key
step "Generating a secret key"
SECRET_KEY=$(venv/bin/python3 -c "import secrets; print(secrets.token_hex(32))")
echo "Generated (used to sign login sessions -- keep this file private)."

# Quick local test
step "Quick local test"
PENTRACKER_PASSWORD="$PENTRACKER_PASSWORD" PENTRACKER_SECRET_KEY="$SECRET_KEY" \
    venv/bin/python3 -c "
import database as db
db.init_db()
db.migrate_db()
db.seed_sample_data()
print('Database ready.')
"

# Set up the systemd service (optional but recommended)
step "Set up as an always-on service (systemd)"
read -rp "Set this up now so it runs in the background and survives reboots? [Y/n] " SETUP_SYSTEMD
SETUP_SYSTEMD=${SETUP_SYSTEMD:-Y}

if [[ "$SETUP_SYSTEMD" =~ ^[Yy] ]]; then
    CURRENT_USER=$(whoami)
    SERVICE_FILE="/etc/systemd/system/wakil-nibs.service"

    read -rp "Port to run on [8000]: " GUNICORN_PORT
    GUNICORN_PORT=${GUNICORN_PORT:-8000}

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Wakil Nibs
After=network.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/venv/bin"
Environment="PENTRACKER_PASSWORD=$PENTRACKER_PASSWORD"
Environment="PENTRACKER_SECRET_KEY=$SECRET_KEY"
Environment="PENTRACKER_TRUST_PROXY=1"
ExecStart=$PROJECT_DIR/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:$GUNICORN_PORT app:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable --now wakil-nibs
    sleep 1

    if systemctl is-active --quiet wakil-nibs; then
        echo "Service is running. Check anytime with: sudo systemctl status wakil-nibs"
    else
        note "Service didn't start -- check the log with: sudo journalctl -u wakil-nibs -n 50"
        exit 1
    fi
else
    note "Skipped. You can run it manually anytime with:"
    echo "  PENTRACKER_PASSWORD='...' PENTRACKER_SECRET_KEY='...' venv/bin/gunicorn --bind 127.0.0.1:8000 app:app"
    GUNICORN_PORT=8000
fi

# Generate an nginx config to review
step "Generate an nginx config"
read -rp "Set this up behind nginx with a domain/subdomain? [Y/n] " SETUP_NGINX
SETUP_NGINX=${SETUP_NGINX:-Y}

if [[ "$SETUP_NGINX" =~ ^[Yy] ]]; then
    read -rp "Domain or subdomain (e.g. pens.yourname.duckdns.org): " SERVER_NAME
    NGINX_CONF="$PROJECT_DIR/wakil-nibs-nginx.conf"

    cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;

    location /static/ {
        alias $PROJECT_DIR/static/;
    }

    location / {
        proxy_pass http://127.0.0.1:$GUNICORN_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

    echo "Wrote $NGINX_CONF -- nothing has been touched in /etc/nginx yet."
    echo
    echo "Review it, then enable it with:"
    if [ -d /etc/nginx/sites-available ]; then
        echo "  sudo cp $NGINX_CONF /etc/nginx/sites-available/wakil-nibs"
        echo "  sudo ln -s /etc/nginx/sites-available/wakil-nibs /etc/nginx/sites-enabled/"
    else
        echo "  sudo cp $NGINX_CONF /etc/nginx/conf.d/wakil-nibs.conf"
    fi
    echo "  sudo nginx -t"
    echo "  sudo systemctl reload nginx"
    echo
    echo "Then, once $SERVER_NAME points at this machine, add HTTPS with:"
    echo "  sudo certbot --nginx -d $SERVER_NAME"
fi

# Done
step "Done"
echo "Local test:      curl http://127.0.0.1:${GUNICORN_PORT}/"
if [[ "$SETUP_SYSTEMD" =~ ^[Yy] ]]; then
    echo "Service status:  sudo systemctl status wakil-nibs"
    echo "Service logs:    sudo journalctl -u wakil-nibs -n 50"
fi
echo
note "Password and secret key are saved in the systemd service file only"
note "($([ -f /etc/systemd/system/wakil-nibs.service ] && echo "/etc/systemd/system/wakil-nibs.service" || echo "not written -- see manual command above")) -- write your password down somewhere safe, it isn't shown again."
