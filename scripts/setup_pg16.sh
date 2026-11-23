#!/bin/sh
set -e
mkdir -p /var/run/postgresql
chown -R postgres:postgres /var/run/postgresql
chmod 775 /var/run/postgresql

echo "local all all trust" > /etc/postgresql/16/main/pg_hba.conf
echo "host all all all trust" >> /etc/postgresql/16/main/pg_hba.conf
echo "listen_addresses = '*'" >> /etc/postgresql/16/main/postgresql.conf

exec su - postgres -c "/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main -c config_file=/etc/postgresql/16/main/postgresql.conf"
