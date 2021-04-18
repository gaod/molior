#!/bin/sh

if [ -z "$DEBSIGN_EMAIL" ]; then
    DEBSIGN_EMAIL=debsign@molior.info
fi

if [ -z "$DEBSIGN_NAME" ]; then
    DEBSIGN_NAME="Molior Debsign"
fi

create-molior-keys $DEBSIGN_NAME $DEBSIGN_EMAIL
su molior -c "gpg1 --armor --export $DEBSIGN_EMAIL | gpg1 --import --no-default-keyring --keyring=trustedkeys.gpg"

sed -i 's/127.0.0.1/molior/' /etc/molior/molior.yml
sed -i "s/\( \+apt_url: \).*/\1'http:\/\/aptly:3142'/" /etc/molior/molior.yml
sed -i "s/\( \+api_url: \).*/\1'http:\/\/aptly:8080\/api'/" /etc/molior/molior.yml

# wait a bit for database
sleep 5

/usr/lib/molior/db-upgrade.sh
su molior -c "/usr/bin/python3 -m molior.molior.server --host=molior --port=8888"
