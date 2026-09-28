#!/bin/sh
set -e

echo "Starting test database..."
docker compose up -d test-db

echo "Waiting for test database..."
until docker compose exec -T test-db pg_isready -U arthatantra -d arthatantra_test > /dev/null 2>&1
do
    sleep 1
done

echo "Running migrations..."
docker compose run --rm \
  --no-deps \
  -e DATABASE_URL=postgresql+psycopg://arthatantra:arthatantra@test-db:5432/arthatantra_test \
  app alembic upgrade head

echo "Starting FastAPI against test database..."
docker compose --profile test up -d app-test

echo "Test application available at http://localhost:8001/docs"
