#!/bin/bash
set -e

echo "========================================="
echo " Starting AgroBridge Backend Container "
echo "========================================="

# Determine MySQL database connection parameters
DB_HOST=${DB_HOST:-syntrrix-db.cru0g2ggwfsb.ap-south-1.rds.amazonaws.com}
DB_PORT=${DB_PORT:-3306}

echo "Checking connection to MySQL database at ${DB_HOST}:${DB_PORT}..."

python << END
import sys
import time
import socket

host = "${DB_HOST}"
port = int("${DB_PORT}")

start_time = time.time()
connected = False

while time.time() - start_time < 30:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((host, port))
        s.close()
        connected = True
        print(f"Successfully connected to MySQL database at {host}:{port}")
        break
    except Exception as e:
        print(f"Database at {host}:{port} is not ready yet ({e}), retrying in 2 seconds...")
        time.sleep(2)

if not connected:
    print(f"Warning: Unable to reach MySQL database at {host}:{port}.")
    print("Please check your AWS RDS security groups (allow port 3306 inbound) and DB credentials in .env!")
END

echo "Collecting static files..."
python manage.py collectstatic --noinput || echo "Static file collection notice: proceeding."

echo "Verifying database schema..."
python ensure_db_schema.py || echo "Schema check notice: proceeding."

echo "Applying database migrations..."
python manage.py migrate --fake-initial --noinput || echo "Migrations notice: proceeding."

echo "Starting server..."
exec "$@"
