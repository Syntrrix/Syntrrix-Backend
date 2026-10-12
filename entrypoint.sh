#!/bin/bash
set -e

echo "========================================="
echo " Starting AgroBridge Backend Container "
echo "========================================="

# Determine MySQL database connection parameters
DB_HOST=${DB_HOST:-db}
DB_PORT=${DB_PORT:-3306}

echo "Checking connection to MySQL database at ${DB_HOST}:${DB_PORT}..."

python << END
import sys
import time
import socket

host = "${DB_HOST}"
port = int("${DB_PORT}")

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
start_time = time.time()
connected = False

while time.time() - start_time < 30:
    try:
        s.connect((host, port))
        s.close()
        connected = True
        print(f"Successfully connected to MySQL database at {host}:{port}")
        break
    except (socket.error, ConnectionRefusedError):
        print(f"Database at {host}:{port} is not ready yet, retrying in 2 seconds...")
        time.sleep(2)

if not connected:
    print(f"Warning: Unable to reach MySQL database at {host}:{port}.")
    print(f"If running standalone container, set DB_HOST=host.docker.internal or run via docker-compose up!")
END

echo "Collecting static files..."
python manage.py collectstatic --noinput || echo "Static file collection notice: proceeding."

echo "Verifying database schema..."
python ensure_db_schema.py || echo "Schema check notice: proceeding."

echo "Applying database migrations..."
python manage.py migrate --fake-initial --noinput || echo "Migrations notice: proceeding."

echo "Starting server..."
exec "$@"
