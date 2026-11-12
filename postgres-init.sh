#!/bin/sh
# Only on the first initialisation of the volume. The site has a user of its own, not the superuser.
psql -v ON_ERROR_STOP=1 -U postgres -v password="$WEB_DB_PASSWORD" <<'SQL'
CREATE ROLE bitmentor LOGIN PASSWORD :'password';
CREATE DATABASE bitmentor OWNER bitmentor;
SQL
