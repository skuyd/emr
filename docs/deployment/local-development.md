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

With the development settings active, prepare the local fixture account and run
the application in this order:

```powershell
python manage.py migrate
python manage.py seed_development_account
python manage.py runserver
```

The fixture command is available only with `DEBUG=True` and
`OTP_PROVIDER=development`. It creates or refreshes the local account for
`18000000000` with password `123321`; the local OTP is `230412`.

The compose file binds PostgreSQL, Redis, and MinIO only to `127.0.0.1`, uses
named volumes, initializes PostgreSQL `pg_trgm`, and creates a private,
versioned MinIO bucket. It intentionally contains no anonymous/public bucket
policy. MinIO OSS is archived and is included only for isolated local
development; production must use a maintained S3-compatible service.

To stop local services without deleting their named volumes:

```powershell
docker compose down
```

Production/closed-trial image deployment, managed S3, SMS, monitoring, encrypted
backup, isolated restore, browser and performance procedures are defined in
[`production-runbook.md`](production-runbook.md). The production stack must not receive real users
until `docs/verification/release-gate.md` is machine-verified as `PASS`.
