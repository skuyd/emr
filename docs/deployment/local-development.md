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

On Windows, the recommended launcher uses the PaddleOCR environment on the `D:`
drive, applies migrations, checks Django, refreshes the development account,
starts the durable local OCR and development SMS worker in the background, and keeps the web
server in the foreground:

```powershell
.\deploy\start-local.ps1
```

Press `Ctrl+C` to stop both processes. Worker output is written under
`.runtime/logs/`. Use `-NoWorker`, `-NoSeed`, `-Address localhost:8080`, or
`-PythonPath <python.exe>` when the defaults are not appropriate. The launcher
only permits loopback bind addresses.

Alternatively, prepare the local fixture account and run the application
manually in this order:

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

Only `config.settings.dev` loads the repository `.env`; production and tests require
explicit environment variables. On Windows run tests with `$env:PYTHONUTF8 = "1"`
so subprocesses use the same encoding. The local worker consumes pending development
SMS jobs before OCR and between documents; without a worker, password-reset messages
remain queued. A long OCR document can delay local SMS; production uses separate
`ocr` and `control` workers as described in the production runbook.
