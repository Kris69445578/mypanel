#!/usr/bin/env bash
# Removes MyPanel. Server files (/srv/mypanel) and the database are kept unless PURGE=1.
set -u
[ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }
systemctl disable --now mypanel 2>/dev/null
rm -f /etc/systemd/system/mypanel.service /etc/nginx/sites-enabled/mypanel /etc/nginx/sites-available/mypanel /usr/local/bin/mypanel
systemctl daemon-reload; systemctl reload nginx 2>/dev/null
if [ "${PURGE:-0}" = "1" ]; then
  docker ps -aq --filter label=mypanel.managed=1 | xargs -r docker rm -f
  rm -rf /srv/mypanel /var/lib/mypanel
  echo "Containers, server files and database removed."
else
  echo "Kept /srv/mypanel and /var/lib/mypanel and managed containers (PURGE=1 removes them)."
fi
rm -rf /etc/mypanel /opt/mypanel
echo "MyPanel removed."
