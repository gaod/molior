#!/bin/sh

if [ -z "$REPOSIGN_EMAIL" ]; then
    REPOSIGN_EMAIL=debsign@molior.info
fi

if [ -z "$REPOSIGN_NAME" ]; then
    REPOSIGN_NAME="Molior Reposign"
fi

create-aptly-keys $REPOSIGN_NAME $REPOSIGN_EMAIL

sed -i 's/80/3142/' /etc/nginx/sites-enabled/aptly
sed -i -e 's/localhost/aptly/' -e 's/8000/8001/' /etc/nginx/sites-enabled/aptlyapi

su - aptly -c "HOME=/var/lib/aptly /usr/bin/aptly api serve -gpg-provider=internal -listen aptly:8001"
