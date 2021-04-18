#!/bin/sh

if [ -z "$APTLY_USER" ]; then
    APTLY_USER=molior
fi

if [ -z "$APTLY_PASS" ]; then
    APTLY_PASS=molior-dev
fi

create-aptly-passwd $APTLY_USER $APTLY_PASS

exec /usr/sbin/nginx
