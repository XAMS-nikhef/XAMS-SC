#!/usr/bin/env bash
# Install or update the XAMS plotting VM (plotit-xams). See DESIGN.md §12 and
# docs/vm.md, which describes what it does and how to run it.
#
# First time, on the VM:
#
#     sudo git clone https://github.com/XAMS-nikhef/XAMS-SC.git /opt/xams-sc
#     sudo LABPC_IP=<lab PC IP> WATCHDOG_EMAIL=<address> SMTP_HOST=<relay:port> \
#          /opt/xams-sc/tools/vm/install_vm.sh
#
# Every time after that — to update, or to change a setting:
#
#     sudo /opt/xams-sc/tools/vm/install_vm.sh
#     sudo LABPC_IP=<new IP> /opt/xams-sc/tools/vm/install_vm.sh
#
# IDEMPOTENT. Every step checks before it acts, so running it twice changes
# nothing, and running it after a `git pull` applies what changed. Settings
# given once are remembered in /etc/xams-vm/vm.env; passwords are generated
# once and kept in /etc/xams-vm/credentials (root only). Nothing secret goes
# into git or onto the command line.
#
# What it installs: PostgreSQL 18 (TLS, three least-privilege roles), Grafana
# (loopback only, provisioned datasource, optional watchdog alert), nginx (HTTPS
# in front of Grafana), and a ufw firewall. Ubuntu/Debian only.
#
# Settings (environment variables, remembered after the first run):
#   LABPC_IP        the lab PC's address; the ONLY host allowed to write.
#                   Empty = no writer access yet, everything else installs.
#   SERVER_NAME     public host name              (default: hostname -f)
#   ANON_VIEW       yes = anyone who reaches 443 sees the dashboards read-only;
#                   no  = Grafana login required  (default: yes)
#   SMTP_HOST       relay as host:port, for the watchdog email (default: none)
#   SMTP_FROM       sender address                (default: xams-watchdog@<SERVER_NAME>)
#   WATCHDOG_EMAIL  who is told when the lab PC goes quiet (default: none = no rule)
#   USE_UFW         yes/no, manage the host firewall (default: yes)
#   ACME            yes = get a browser-trusted web certificate by ACME (certbot),
#                   renewed automatically (default: no = self-signed, or your
#                   own in /etc/xams-vm/web)
#   ACME_SERVER     the ACME directory URL (default: Let's Encrypt, which needs
#                   port 80 reachable from the internet; Nikhef uses HARICA)
#   ACME_EMAIL      account contact address (default: none)
#
# A CA that needs External Account Binding (HARICA does) takes its Key ID and
# HMAC key from /etc/xams-vm/acme-eab, a root-only file you write yourself:
#     ACME_EAB_KID=...
#     ACME_EAB_HMAC=...
# They are used once, to register the account, and the file is then deleted:
# renewals use the registered account. They never pass through vm.env, git or
# a command you type (certbot does receive them as arguments for the few
# seconds it runs, which only root and the one login user could see).

set -euo pipefail

REPO_DIR=/opt/xams-sc
REPO_URL=https://github.com/XAMS-nikhef/XAMS-SC.git
CONF_DIR=/etc/xams-vm
ENV_FILE=$CONF_DIR/vm.env
CRED_FILE=$CONF_DIR/credentials
PG_MAJOR=18
PG_ETC=/etc/postgresql/$PG_MAJOR/main
PG_TLS=$CONF_DIR/pg
WEB_TLS=$CONF_DIR/web
GRAFANA_URL=http://127.0.0.1:3000

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
warn() { printf '\033[33m   WARNING: %s\033[0m\n' "$*"; }
die()  { printf '\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
. /etc/os-release
[[ ${ID:-} == ubuntu || ${ID:-} == debian ]] || die "Ubuntu/Debian only (this is ${ID:-unknown})"

# ------------------------------------------------------------------ the repo
# Pull FIRST, then re-execute the fresh copy. Bash reads a script as it runs,
# so pulling a new version of this file underneath itself would run half of
# each.
if [[ -z ${XAMS_REEXEC:-} ]]; then
    say "Repository $REPO_DIR"
    command -v git >/dev/null || { apt-get update -q; apt-get install -yq git; }
    if [[ -d $REPO_DIR/.git ]]; then
        git -C "$REPO_DIR" pull --ff-only -q
    else
        git clone -q "$REPO_URL" "$REPO_DIR"
    fi
    note "at $(git -C "$REPO_DIR" log --oneline -1)"
    XAMS_REEXEC=1 exec "$REPO_DIR/tools/vm/install_vm.sh" "$@"
fi

# ------------------------------------------------------------------ settings
install -d -m 755 "$CONF_DIR"
# Command-line environment wins over the remembered file.
declare -A GIVEN=()
for k in LABPC_IP SERVER_NAME ANON_VIEW SMTP_HOST SMTP_FROM WATCHDOG_EMAIL USE_UFW ACME ACME_SERVER ACME_EMAIL; do
    if [[ -n ${!k+x} ]]; then GIVEN[$k]=${!k}; fi
done
if [[ -f $ENV_FILE ]]; then . "$ENV_FILE"; fi
for k in "${!GIVEN[@]}"; do printf -v "$k" '%s' "${GIVEN[$k]}"; done

LABPC_IP=${LABPC_IP:-}
SERVER_NAME=${SERVER_NAME:-$(hostname -f)}
ANON_VIEW=${ANON_VIEW:-yes}
SMTP_HOST=${SMTP_HOST:-}
SMTP_FROM=${SMTP_FROM:-xams-watchdog@$SERVER_NAME}
WATCHDOG_EMAIL=${WATCHDOG_EMAIL:-}
USE_UFW=${USE_UFW:-yes}
# LETSENCRYPT / LE_EMAIL were the names before ACME_SERVER existed; an older
# vm.env still carries them.
ACME=${ACME:-${LETSENCRYPT:-no}}
ACME_SERVER=${ACME_SERVER:-https://acme-v02.api.letsencrypt.org/directory}
ACME_EMAIL=${ACME_EMAIL:-${LE_EMAIL:-}}

if [[ -n $LABPC_IP && ! $LABPC_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    die "LABPC_IP must be one IPv4 address, got '$LABPC_IP'"
fi

cat > "$ENV_FILE" <<EOF
# Remembered by tools/vm/install_vm.sh. Edit, or pass as environment, and rerun.
LABPC_IP='$LABPC_IP'
SERVER_NAME='$SERVER_NAME'
ANON_VIEW='$ANON_VIEW'
SMTP_HOST='$SMTP_HOST'
SMTP_FROM='$SMTP_FROM'
WATCHDOG_EMAIL='$WATCHDOG_EMAIL'
USE_UFW='$USE_UFW'
ACME='$ACME'
ACME_SERVER='$ACME_SERVER'
ACME_EMAIL='$ACME_EMAIL'
EOF
chmod 644 "$ENV_FILE"

# Generated once, then kept. Alphanumeric only, so they are safe inside SQL
# literals, systemd Environment= lines and a DSN without any quoting.
genpw() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 24; }
if [[ ! -f $CRED_FILE ]]; then
    umask 077
    cat > "$CRED_FILE" <<EOF
PG_ADMIN_PW=$(genpw)
PG_WRITER_PW=$(genpw)
PG_READER_PW=$(genpw)
GRAFANA_ADMIN_PW=$(genpw)
EOF
    umask 022
fi
chmod 600 "$CRED_FILE"
. "$CRED_FILE"

# ------------------------------------------------------------------ packages
say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq ca-certificates curl gnupg openssl nginx ufw python3 >/dev/null

install -d -m 755 /etc/apt/keyrings /usr/share/postgresql-common/pgdg
if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $VERSION_CODENAME-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
fi
if [[ ! -f /etc/apt/sources.list.d/grafana.list ]]; then
    curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/grafana.gpg
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
        > /etc/apt/sources.list.d/grafana.list
fi
apt-get update -q
apt-get install -yq "postgresql-$PG_MAJOR" grafana >/dev/null
note "$(psql --version), grafana $(dpkg-query -W -f='${Version}' grafana)"

# ------------------------------------------------------------------ PostgreSQL
say "PostgreSQL $PG_MAJOR"
# Self-signed and long-lived: the lab PC pins exactly this certificate
# (sslmode=verify-full), so no CA is involved and nothing expires on us.
install -d -m 750 -o postgres -g postgres "$PG_TLS"
if [[ ! -f $PG_TLS/server.crt ]]; then
    openssl req -new -x509 -days 3650 -nodes -subj "/CN=$SERVER_NAME" \
        -addext "subjectAltName=DNS:$SERVER_NAME" \
        -keyout "$PG_TLS/server.key" -out "$PG_TLS/server.crt" 2>/dev/null
    note "generated the database TLS certificate for $SERVER_NAME"
fi
chown postgres:postgres "$PG_TLS"/server.*
chmod 600 "$PG_TLS/server.key"
# The public half, where anyone may fetch it for the lab PC.
install -m 644 "$PG_TLS/server.crt" "$CONF_DIR/plotit-xams-ca.crt"

cat > "$PG_ETC/conf.d/xams.conf" <<EOF
# Managed by $REPO_DIR/tools/vm/install_vm.sh — edits are overwritten.
# Listening everywhere is safe only together with pg_hba.conf and the
# firewall, which both admit the lab PC alone.
listen_addresses = '*'
ssl = on
ssl_cert_file = '$PG_TLS/server.crt'
ssl_key_file  = '$PG_TLS/server.key'
password_encryption = scram-sha-256
EOF

[[ -f $PG_ETC/pg_hba.conf.orig ]] || cp "$PG_ETC/pg_hba.conf" "$PG_ETC/pg_hba.conf.orig"
{
    echo "# Managed by $REPO_DIR/tools/vm/install_vm.sh — edits are overwritten."
    echo "# Original: pg_hba.conf.orig"
    echo "local    all   postgres                    peer"
    echo "local    all   all                         peer"
    echo "host     xams  xams_reader  127.0.0.1/32   scram-sha-256"
    echo "host     xams  xams_admin   127.0.0.1/32   scram-sha-256"
    if [[ -n $LABPC_IP ]]; then
        echo "# The lab PC, and only over TLS."
        echo "hostssl  xams  xams_writer  $LABPC_IP/32   scram-sha-256"
    fi
} > "$PG_ETC/pg_hba.conf"
systemctl enable -q postgresql
systemctl restart postgresql

pg() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 "$@"; }
pg <<EOF
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'xams_admin')  THEN CREATE ROLE xams_admin  LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'xams_writer') THEN CREATE ROLE xams_writer LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'xams_reader') THEN CREATE ROLE xams_reader LOGIN; END IF;
END \$\$;
ALTER ROLE xams_admin  PASSWORD '$PG_ADMIN_PW';
ALTER ROLE xams_writer PASSWORD '$PG_WRITER_PW';
ALTER ROLE xams_reader PASSWORD '$PG_READER_PW';
EOF
if ! pg -tAc "SELECT 1 FROM pg_database WHERE datname = 'xams'" | grep -q 1; then
    sudo -u postgres createdb -O xams_admin xams
    note "created database xams"
fi
# As the OWNER, so the unique index is built. On 17 September 2026 it was
# not, for want of permission, and every upsert failed (pg_writer.py).
pg -d xams -c 'SET ROLE xams_admin' -f "$REPO_DIR/sql/schema.sql"
pg -d xams <<'EOF'
GRANT USAGE ON SCHEMA public TO xams_writer, xams_reader;
-- The writer INSERTs, and only into what the lab PC sends. No UPDATE, no
-- DELETE: a compromised lab PC credential cannot rewrite history here.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM xams_writer;
GRANT INSERT ON meas, alarm_events TO xams_writer;
-- ON CONFLICT (t, channel, src) must look for the existing row, so it needs
-- SELECT on exactly those columns. Without it every write is "permission
-- denied for table meas" (found on the first connection, 23 September 2026).
-- The key columns only: the writer still cannot read a single value.
GRANT SELECT (t, channel, src) ON meas TO xams_writer;
-- The RGA summaries (docs/drivers/rga.md, 13 October 2026): INSERT, and
-- SELECT on the key columns for ON CONFLICT, as for meas. Never UPDATE or
-- DELETE: a corrected summary is rewritten here by an admin, not the lab PC.
GRANT INSERT ON rga_saves, rga_summary TO xams_writer;
GRANT SELECT (file) ON rga_saves TO xams_writer;
GRANT SELECT (file, mass) ON rga_summary TO xams_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO xams_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE xams_admin IN SCHEMA public
    GRANT SELECT ON TABLES TO xams_reader;
EOF
note "roles xams_admin / xams_writer (INSERT only) / xams_reader (SELECT only)"

# ------------------------------------------------------------------ Grafana
say "Grafana"
# Settings go in as environment on the service rather than as edits to
# grafana.ini: nothing to merge on a package upgrade, and one file says
# everything this install changed.
install -d -m 755 /etc/systemd/system/grafana-server.service.d
anon=false; [[ $ANON_VIEW == yes ]] && anon=true
smtp=false; [[ -n $SMTP_HOST ]] && smtp=true
umask 077
cat > /etc/systemd/system/grafana-server.service.d/xams.conf <<EOF
# Managed by $REPO_DIR/tools/vm/install_vm.sh — edits are overwritten.
[Service]
Environment=GF_SERVER_HTTP_ADDR=127.0.0.1
Environment=GF_SERVER_HTTP_PORT=3000
Environment=GF_SERVER_DOMAIN=$SERVER_NAME
Environment=GF_SERVER_ROOT_URL=https://$SERVER_NAME/
Environment=GF_SECURITY_ADMIN_PASSWORD=$GRAFANA_ADMIN_PW
Environment=GF_SECURITY_COOKIE_SECURE=true
Environment=GF_USERS_ALLOW_SIGN_UP=false
Environment=GF_AUTH_ANONYMOUS_ENABLED=$anon
Environment=GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
Environment=GF_ANALYTICS_REPORTING_ENABLED=false
Environment=GF_SMTP_ENABLED=$smtp
Environment=GF_SMTP_HOST=$SMTP_HOST
Environment=GF_SMTP_FROM_ADDRESS=$SMTP_FROM
Environment="GF_SMTP_FROM_NAME=XAMS watchdog"
Environment=XAMS_PG_PASSWORD=$PG_READER_PW
Environment=XAMS_WATCHDOG_EMAIL=$WATCHDOG_EMAIL
EOF
umask 022

install -d -m 755 /etc/grafana/provisioning/datasources /etc/grafana/provisioning/alerting
ln -sfn "$REPO_DIR/grafana/provisioning-vm/datasources/xams.yaml" \
        /etc/grafana/provisioning/datasources/xams.yaml
if [[ -n $WATCHDOG_EMAIL ]]; then
    ln -sfn "$REPO_DIR/grafana/provisioning-vm/alerting/watchdog.yaml" \
            /etc/grafana/provisioning/alerting/xams-watchdog.yaml
    note "watchdog alert -> $WATCHDOG_EMAIL"
else
    rm -f /etc/grafana/provisioning/alerting/xams-watchdog.yaml
    warn "no WATCHDOG_EMAIL: the 'lab PC silent' alert is NOT installed"
fi

systemctl daemon-reload
systemctl enable -q grafana-server
systemctl restart grafana-server
for _ in $(seq 60); do
    curl -fs "$GRAFANA_URL/api/health" >/dev/null && break
    sleep 1
done
curl -fs "$GRAFANA_URL/api/health" >/dev/null \
    || die "Grafana did not come up; see journalctl -u grafana-server"

# GF_SECURITY_ADMIN_PASSWORD only applies when the admin user is first
# created. Keep the stored password true on every later run as well.
if [[ $(curl -s -o /dev/null -w '%{http_code}' -u "admin:$GRAFANA_ADMIN_PW" "$GRAFANA_URL/api/org") != 200 ]]; then
    printf '%s\n' "$GRAFANA_ADMIN_PW" | sudo -u grafana grafana-cli \
        --homepath /usr/share/grafana --config /etc/grafana/grafana.ini \
        admin reset-admin-password --password-from-stdin >/dev/null
    note "reset the Grafana admin password to the stored one"
fi

# Dashboards ONCE. After that, Grafana on this VM owns them and people edit
# them in the UI; loading again would silently revert those edits. To force
# it, delete the marker or run save_dashboard.py --load by hand.
if [[ ! -f $CONF_DIR/.dashboards-loaded ]]; then
    GRAFANA_PASSWORD=$GRAFANA_ADMIN_PW python3 "$REPO_DIR/tools/save_dashboard.py" \
        --load --url "$GRAFANA_URL" --user admin
    touch "$CONF_DIR/.dashboards-loaded"
else
    # A dashboard added to git since (the RGA one, 13 October 2026) is loaded;
    # one that is already here is never touched.
    GRAFANA_PASSWORD=$GRAFANA_ADMIN_PW python3 "$REPO_DIR/tools/save_dashboard.py" \
        --load --missing --url "$GRAFANA_URL" --user admin
    note "dashboards: new ones loaded; existing ones (and their UI edits) untouched"
fi

# ------------------------------------------------------------------ nginx
say "nginx"
install -d -m 755 "$WEB_TLS" /var/www/acme
if [[ ! -f $WEB_TLS/fullchain.crt ]]; then
    # The fallback: a real certificate from Nikhef CT goes here under the same
    # names, or ACME=yes replaces it with one that browsers trust.
    openssl req -new -x509 -days 825 -nodes -subj "/CN=$SERVER_NAME" \
        -addext "subjectAltName=DNS:$SERVER_NAME" \
        -keyout "$WEB_TLS/privkey.key" -out "$WEB_TLS/fullchain.crt" 2>/dev/null
fi
chmod 600 "$WEB_TLS/privkey.key"

# write_nginx <certificate> <key>
write_nginx() {
cat > /etc/nginx/sites-available/xams-grafana <<EOF
# Managed by $REPO_DIR/tools/vm/install_vm.sh — edits are overwritten.
map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $SERVER_NAME;

    # Let's Encrypt proves control of the name by fetching a file from here,
    # over plain http. Everything else goes to https.
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/acme;
    }
    location / {
        return 301 https://$SERVER_NAME\$request_uri;
    }
}

server {
    # http2 on the listen line: Ubuntu 24.04 ships nginx 1.24, which
    # predates the separate "http2 on;" directive. (No backticks in here:
    # this heredoc is unquoted, so they would run as commands.)
    listen 443 ssl http2 default_server;
    listen [::]:443 ssl http2 default_server;
    server_name $SERVER_NAME;

    ssl_certificate     $1;
    ssl_certificate_key $2;
    ssl_protocols       TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_set_header Host \$host;
        proxy_pass $GRAFANA_URL;
    }
    location /api/live/ {
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$host;
        proxy_pass $GRAFANA_URL;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/xams-grafana /etc/nginx/sites-enabled/xams-grafana
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl enable -q nginx
systemctl reload nginx || systemctl restart nginx
}

ACME_LIVE=/etc/letsencrypt/live/$SERVER_NAME
EAB_FILE=$CONF_DIR/acme-eab
# The certificate must come from the CA configured NOW. A lineage from an
# earlier server (a Let's Encrypt attempt, say) is not reused.
acme_current() {
    [[ -f $ACME_LIVE/fullchain.pem ]] \
        && grep -qF "server = $ACME_SERVER" "/etc/letsencrypt/renewal/$SERVER_NAME.conf" 2>/dev/null
}
if [[ $ACME == yes ]] && acme_current; then
    write_nginx "$ACME_LIVE/fullchain.pem" "$ACME_LIVE/privkey.pem"
    note "ACME certificate from $(awk -F'/' '{print $3}' <<<"$ACME_SERVER"); certbot.timer renews it"
elif [[ $ACME == yes ]]; then
    # nginx must already be serving port 80 in case the CA asks for an
    # http-01 challenge, so start on the fallback and switch once issued.
    write_nginx "$WEB_TLS/fullchain.crt" "$WEB_TLS/privkey.key"
    apt-get install -yq certbot >/dev/null
    acme_args=(--server "$ACME_SERVER")
    if [[ -n $ACME_EMAIL ]]; then acme_args+=(--email "$ACME_EMAIL"); else acme_args+=(--register-unsafely-without-email); fi
    if [[ -f $EAB_FILE ]]; then
        chmod 600 "$EAB_FILE"
        ACME_EAB_KID=$(sed -n 's/^ACME_EAB_KID=//p' "$EAB_FILE" | tr -d '[:space:]')
        ACME_EAB_HMAC=$(sed -n 's/^ACME_EAB_HMAC=//p' "$EAB_FILE" | tr -d '[:space:]')
        [[ -n $ACME_EAB_KID && -n $ACME_EAB_HMAC ]] \
            || die "$EAB_FILE needs both ACME_EAB_KID= and ACME_EAB_HMAC= lines"
        acme_args+=(--eab-kid "$ACME_EAB_KID" --eab-hmac-key "$ACME_EAB_HMAC")
    fi
    # --cert-name pins the lineage directory; --force-renewal replaces a
    # lineage from another CA rather than keeping it because it is "not due".
    if certbot certonly -q --non-interactive --agree-tos "${acme_args[@]}" \
            --cert-name "$SERVER_NAME" --force-renewal \
            --webroot -w /var/www/acme -d "$SERVER_NAME"; then
        write_nginx "$ACME_LIVE/fullchain.pem" "$ACME_LIVE/privkey.pem"
        note "ACME certificate obtained from $(awk -F'/' '{print $3}' <<<"$ACME_SERVER"); certbot.timer renews it"
        if [[ -f $EAB_FILE ]]; then
            rm -f "$EAB_FILE"
            note "account registered; removed $EAB_FILE (renewals do not need it)"
        fi
    else
        warn "ACME failed; keeping the current certificate. Details: /var/log/letsencrypt/letsencrypt.log"
    fi
else
    write_nginx "$WEB_TLS/fullchain.crt" "$WEB_TLS/privkey.key"
    if openssl x509 -in "$WEB_TLS/fullchain.crt" -noout -issuer | grep -q "CN *= *$SERVER_NAME\$"; then
        warn "self-signed web certificate; browsers will warn. Rerun with ACME=yes, or put a real one in $WEB_TLS"
    fi
fi

# Renewal reloads nginx, or it would go on serving the expired certificate.
if [[ -d /etc/letsencrypt/renewal-hooks/deploy ]]; then
    printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
    chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
fi

# ------------------------------------------------------------------ firewall
if [[ $USE_UFW == yes ]]; then
    say "Firewall (ufw)"
    # ssh FIRST, so enabling the firewall can never cut off this session —
    # and only if sshd is on the port the OpenSSH profile opens.
    ss -tlnH | awk '{print $4}' | grep -qE ':22$' \
        || die "sshd is not on port 22; enabling ufw could lock you out. Rerun with USE_UFW=no"
    ufw allow OpenSSH >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    # Drop any earlier 5432 rule (the lab PC's address may have changed),
    # then admit the lab PC alone.
    while read -r n; do
        ufw --force delete "$n" >/dev/null
    done < <(ufw status numbered | grep -E '^\[ *[0-9]+\] 5432' | grep -oE '^\[ *[0-9]+' | tr -d '[ ' | sort -rn)
    if [[ -n $LABPC_IP ]]; then ufw allow from "$LABPC_IP" to any port 5432 proto tcp >/dev/null; fi
    ufw default deny incoming >/dev/null
    ufw --force enable >/dev/null
    ufw status | sed 's/^/   /'
fi

# ------------------------------------------------------------------ report
say "Checks"
ss -tlnH | awk '{print $4}' | { grep -E ':(3000|5432|443|80)$' || true; } | sort -u | sed 's/^/   listening /'
free_gb=$(df -BG --output=avail /var/lib/postgresql | tail -1 | tr -dc 0-9)
note "disk free for the database: ${free_gb} GB (about 0.1 GB/day)"
if (( free_gb < 100 )); then warn "under 100 GB free: roughly $((free_gb * 10)) days of data"; fi

say "Done"
cat <<EOF
   Dashboards:  https://$SERVER_NAME/   (admin password: sudo cat $CRED_FILE)

   On the lab PC (it has no ssh to this VM, only port 5432, so carry
   these two over by hand, from a machine that does):
   1. the database certificate, public -> config/plotit-xams-ca.crt:
        ssh plotit-xams cat $CONF_DIR/plotit-xams-ca.crt
   2. add to config/secrets.yaml, with the password from
        ssh plotit-xams sudo grep WRITER $CRED_FILE
        postgres_remote:
          host: "$SERVER_NAME"
          port: 5432
          database: "xams"
          user: "xams_writer"
          password: "<PG_WRITER_PW>"
          sslmode: "verify-full"
          sslrootcert: "config/plotit-xams-ca.crt"
   3. xams-ctl restart, and look for "[nikhef-vm] connected" in logs/sinks.log
EOF
if [[ -z $LABPC_IP ]]; then warn "LABPC_IP not set: the lab PC cannot write yet. Rerun with sudo LABPC_IP=<ip> $0"; fi
exit 0
