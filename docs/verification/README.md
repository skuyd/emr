# Foundation verification evidence

This directory separates evidence that ran on the local SQLite/Django test
environment from static contracts and from checks that cannot be claimed on
this workstation. It intentionally excludes user identifiers and patient
labels; acceptance fixtures use synthetic values only.

## Evidence categories

| Category | Status | Notes |
| --- | --- | --- |
| SQLite HTTP acceptance and Django tests | RUN | See `ac00-ac01.md`. |
| Compose/static service contract | RUN | Checked from source without Docker. |
| Django migration and system checks | RUN | SQLite migration state and checks recorded in `ac00-ac01.md`. |
| Docker service lifecycle | NOT RUN | Docker CLI was not installed. |
| PostgreSQL/`pg_trgm` runtime | NOT RUN | Docker and `psql` were not installed. |
| Redis/MinIO runtime | NOT RUN | Docker CLI was not installed. |
| Chrome interaction smoke | RUN | AC-00/AC-01 completed in local Chrome; see `ac00-ac01.md`. |

`compose.yaml` is for isolated local development only. MinIO OSS is archived;
production must use a maintained S3-compatible service.
