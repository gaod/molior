#!/bin/sh

sed -i -e '/::/d' -e 's/localhost/molior/' /etc/nginx/sites-enabled/molior-web

# FIXME: remove default site

exec /usr/sbin/nginx
