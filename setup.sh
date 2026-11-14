#!/usr/bin/env bash
# setup.sh — SSOUyuni installation script
# Run as root on Ubuntu 22.04 LTS or 24.04 LTS.
# Usage: sudo bash setup.sh
set -euo pipefail

INSTALL_DIR="/opt/ssouuni"
SERVICE_USER="ssouuni"
SERVICE_NAME="ssouuni"
NGINX_SITE="ssouuni"

echo "==> SSOUyuni install"

# 1. System packages
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip nginx

# 2. Service user
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /sbin/nologin -d "$INSTALL_DIR" "$SERVICE_USER"
    echo "    created user: $SERVICE_USER"
fi

# 3. Directories
mkdir -p "$INSTALL_DIR/ssl"
mkdir -p "$INSTALL_DIR/venv"

# 4. Copy application files
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
for f in app.py oidc_client.py secrets_client.py uyuni_user_provisioner.py uyuni_web_client.py requirements.txt; do
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
done
echo "    application files copied"

# 5. Python virtual environment
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
echo "    Python venv ready"

# 6. Permissions
chown -R "$SERVICE_USER:" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/ssl"
echo "    permissions set"

# 7. systemd service
cp "$SCRIPT_DIR/deploy/ssouuni.service" /etc/systemd/system/ssouuni.service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
echo "    systemd unit installed"

# 8. nginx
cp "$SCRIPT_DIR/deploy/nginx.conf" /etc/nginx/sites-available/"$NGINX_SITE"
if [ ! -L /etc/nginx/sites-enabled/"$NGINX_SITE" ]; then
    ln -s /etc/nginx/sites-available/"$NGINX_SITE" /etc/nginx/sites-enabled/"$NGINX_SITE"
fi
# Remove nginx default site if it exists (avoid port conflict)
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx --quiet
systemctl restart nginx
echo "    nginx configured"

echo ""
echo "==> Installation complete. Next steps:"
echo ""
echo "  1. Copy .env.example to $INSTALL_DIR/.env and fill in values:"
echo "       cp $SCRIPT_DIR/.env.example $INSTALL_DIR/.env"
echo "       nano $INSTALL_DIR/.env"
echo "       chmod 600 $INSTALL_DIR/.env"
echo "       chown $SERVICE_USER: $INSTALL_DIR/.env"
echo ""
echo "  2. Copy Uyuni CA certificate:"
echo "       # On Uyuni server: podman exec uyuni-server cat /srv/www/htdocs/pub/RHN-ORG-TRUSTED-SSL-CERT"
echo "       # Save output to: $INSTALL_DIR/ssl/uyuni-ca.crt"
echo "       chmod 640 $INSTALL_DIR/ssl/uyuni-ca.crt"
echo "       chown $SERVICE_USER: $INSTALL_DIR/ssl/uyuni-ca.crt"
echo ""
echo "  3. Generate TLS certificate for nginx:"
echo "       openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \\"
echo "           -keyout $INSTALL_DIR/ssl/key.pem \\"
echo "           -out    $INSTALL_DIR/ssl/cert.pem \\"
echo "           -subj   \"/CN=uyuni-proxy.internal\" \\"
echo "           -addext \"subjectAltName=DNS:uyuni-proxy.internal,IP:<YOUR_VM_IP>\""
echo "       chown $SERVICE_USER: $INSTALL_DIR/ssl/*.pem"
echo ""
echo "  4. Add /etc/hosts entry for Uyuni server if needed:"
echo "       echo '<uyuni-ip>  uyuni-server.internal' >> /etc/hosts"
echo ""
echo "  5. Enable Managed Identity on the VM and assign Key Vault Secrets User role."
echo ""
echo "  6. Start the service:"
echo "       systemctl start $SERVICE_NAME"
echo "       systemctl status $SERVICE_NAME"
echo "       journalctl -u $SERVICE_NAME -n 50"
echo ""
echo "  See docs/installation.md for the complete setup guide."
