# Local development services

For a first-time setup, run this single command from the repository root:

```powershell
python deploy/bootstrap_dev_env.py
```

It creates `.env` from `.env.example` using cryptographically random local
secrets and **never overwrites** an existing `.env`. The generated file is
ignored by Git; do not commit it or share it.

Start the isolated local services after that:

```powershell
docker compose --env-file .env up -d
```

The compose file binds PostgreSQL, Redis, and MinIO only to `127.0.0.1`, uses
named volumes, initializes PostgreSQL `pg_trgm`, and creates a private,
versioned MinIO bucket. It intentionally contains no anonymous/public bucket
policy. MinIO OSS is archived and is included only for isolated local
development; production must use a maintained S3-compatible service.

To stop local services without deleting their named volumes:

```powershell
docker compose down
```
