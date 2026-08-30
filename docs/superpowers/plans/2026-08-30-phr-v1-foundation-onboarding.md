# PHR V1 Foundation and Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the runnable Django foundation and complete PRD P00/P01: secure phone OTP login, versioned consent, and exactly one patient space per account.

**Architecture:** A Django 5.2 modular monolith owns HTTP, sessions, database transactions, templates, and tenant permissions. Domain rules live in focused service modules so later upload and worker code reuse the same invariants; PostgreSQL is the production database while SQLite is allowed only for fast unit tests that do not exercise PostgreSQL behavior.

**Tech Stack:** Python 3.11, Django 5.2 LTS, Celery 5.6, Redis, PostgreSQL 18, pytest, pytest-django, vanilla JavaScript and CSS

**Spec:** `docs/superpowers/specs/2026-08-30-phr-v1-system-design.md`

## Global Constraints

- The product is a patient/family PHR, not a hospital EMR.
- Production must refuse to start with the development OTP provider.
- Only mainland China `+86` mobile numbers are accepted.
- OTPs are six digits, expire after five minutes, and lock after five failed attempts.
- Resend limits are 60 seconds, five sends per hour, and fifteen sends per 24 hours per phone; an IP may request at most thirty sends per hour.
- Sessions expire after 24 idle hours and seven absolute days.
- A first-time user must accept privacy, sensitive-information, and upload-authorization records before creating exactly one patient.
- Patient display name is 1–20 visible characters after trimming.
- No analytics or logs may contain raw phone numbers, patient names, files, OCR text, search text, or medical values.
- All user-visible Chinese copy must avoid the medical-judgment words prohibited by PRD section 8.3.

---

### Task 1: Reproducible project skeleton and environment checks

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `manage.py`
- Create: `config/__init__.py`
- Create: `config/settings/__init__.py`
- Create: `config/settings/base.py`
- Create: `config/settings/dev.py`
- Create: `config/settings/test.py`
- Create: `config/urls.py`
- Create: `config/wsgi.py`
- Create: `config/asgi.py`
- Create: `config/celery.py`
- Create: `apps/__init__.py`
- Create: `apps/accounts/__init__.py`
- Create: `apps/accounts/apps.py`
- Create: `apps/accounts/managers.py`
- Create: `apps/accounts/models.py`
- Create: `apps/accounts/migrations/__init__.py`
- Create: `apps/accounts/migrations/0001_initial.py`
- Create: `tests/test_project_configuration.py`

**Interfaces:**
- Consumes: environment variables documented in `.env.example`.
- Produces: `config.settings.test`, `config.settings.dev`, `config.celery.app`, a runnable `manage.py`, and the minimal custom `accounts.Account` model required before the first migration.

- [ ] **Step 1: Write the failing configuration tests**

```python
from django.conf import settings
from django.core.checks import run_checks


def test_project_uses_custom_account_model():
    assert settings.AUTH_USER_MODEL == "accounts.Account"


def test_security_middleware_is_enabled():
    names = set(settings.MIDDLEWARE)
    assert "django.middleware.security.SecurityMiddleware" in names
    assert "django.middleware.csrf.CsrfViewMiddleware" in names


def test_django_system_checks_are_clean():
    assert run_checks() == []
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest tests/test_project_configuration.py -q`

Expected: FAIL because Django/configuration does not exist.

- [ ] **Step 3: Add pinned-compatible dependencies and the Django settings package**

`pyproject.toml` must declare these bounded dependencies:

```toml
[project]
name = "family-phr"
version = "0.1.0"
requires-python = ">=3.11,<3.15"
dependencies = [
  "Django>=5.2.12,<5.3",
  "celery>=5.6.3,<5.7",
  "redis>=5.2,<7",
  "psycopg[binary]>=3.2,<3.4",
  "django-environ>=0.12,<0.13",
  "django-redis>=5.4,<6",
  "django-storages[s3]>=1.14,<2",
  "boto3>=1.35,<2",
  "cryptography>=44,<47",
  "gunicorn>=23,<24",
  "whitenoise>=6.8,<7",
]

[project.optional-dependencies]
test = [
  "pytest>=8.3,<9",
  "pytest-django>=4.10,<5",
  "freezegun>=1.5,<2",
]

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.test"
python_files = ["test_*.py"]
addopts = "--strict-markers --strict-config"
```

`base.py` must configure installed apps, custom user model, templates, session middleware, CSRF, static/media policy, UTC storage and `Asia/Shanghai` display. The minimal `Account` model uses UUID primary keys, `phone_hash` as the unique login identifier, `phone_encrypted`, Django permission mixins, active/staff flags and timestamps. `dev.py` may use SQLite and console OTP. `test.py` uses fast password hashing and an in-memory cache.

- [ ] **Step 4: Run checks and tests to verify GREEN**

Run: `python manage.py check`

Expected: `System check identified no issues`.

Run: `python -m pytest tests/test_project_configuration.py -q`

Expected: 3 passed.

### Task 2: Privacy-safe logging and configuration guard

**Files:**
- Create: `apps/core/__init__.py`
- Create: `apps/core/apps.py`
- Create: `apps/core/logging.py`
- Create: `apps/core/checks.py`
- Create: `tests/core/test_logging.py`
- Create: `tests/core/test_checks.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Consumes: log records and environment-backed settings.
- Produces: `SensitiveDataFilter`, `hash_identifier(value)`, and Django deployment checks `phr.E001`–`phr.E003`.

- [ ] **Step 1: Write failing redaction and production-guard tests**

```python
import logging
from django.core.checks import Tags, run_checks
from django.test import override_settings
from apps.core.logging import SensitiveDataFilter


def test_logging_filter_removes_sensitive_extra_fields():
    record = logging.LogRecord("phr", logging.INFO, __file__, 1, "ok", (), None)
    record.phone = "13800138000"
    record.patient_name = "王女士"
    SensitiveDataFilter().filter(record)
    assert record.phone == "[REDACTED]"
    assert record.patient_name == "[REDACTED]"


@override_settings(DEBUG=False, OTP_PROVIDER="console")
def test_production_rejects_console_otp():
    ids = {error.id for error in run_checks(tags=[Tags.security])}
    assert "phr.E001" in ids
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/core/test_logging.py tests/core/test_checks.py -q`

Expected: FAIL because the filter and checks do not exist.

- [ ] **Step 3: Implement explicit redaction and startup checks**

The filter must redact keys `phone`, `patient_name`, `filename`, `ocr_text`, `search_query`, `lab_name`, and `lab_value`. The production checks must reject `OTP_PROVIDER=console`, a missing secret key, and non-HTTPS cookie settings when `DEBUG=False`.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/core -q`

Expected: all core tests pass with no warnings.

### Task 3: Account, OTP challenge, and throttling domain

**Files:**
- Modify: `apps/accounts/models.py`
- Create: `apps/accounts/phone.py`
- Create: `apps/accounts/crypto.py`
- Create: `apps/accounts/otp.py`
- Create: `apps/accounts/providers.py`
- Create: `apps/accounts/services.py`
- Create: `apps/accounts/admin.py`
- Create: `apps/accounts/migrations/0002_otpchallenge.py`
- Create: `tests/accounts/__init__.py`
- Create: `tests/accounts/fakes.py`
- Create: `tests/accounts/test_phone.py`
- Create: `tests/accounts/test_otp.py`
- Create: `tests/accounts/test_login_service.py`

**Interfaces:**
- Consumes: raw mainland phone input, requester IP, an `SmsProvider`, and current time.
- Produces: `normalize_mainland_phone(raw) -> str`, `request_otp(phone, ip) -> OtpChallenge`, and `verify_otp(phone, code) -> Account`.

- [ ] **Step 1: Write failing phone-normalization tests**

```python
import pytest
from apps.accounts.phone import InvalidPhone, normalize_mainland_phone


@pytest.mark.parametrize("raw", ["13800138000", "+86 138 0013 8000", "86-13800138000"])
def test_normalizes_mainland_mobile(raw):
    assert normalize_mainland_phone(raw) == "+8613800138000"


@pytest.mark.parametrize("raw", ["", "123", "+85291234567", "1380013800a"])
def test_rejects_non_mainland_or_malformed_phone(raw):
    with pytest.raises(InvalidPhone):
        normalize_mainland_phone(raw)
```

- [ ] **Step 2: Verify phone tests RED**

Run: `python -m pytest tests/accounts/test_phone.py -q`

Expected: FAIL because `apps.accounts.phone` does not exist.

- [ ] **Step 3: Implement strict normalization**

Accept only numbers that normalize to `+86` followed by eleven digits beginning with `1`; do not log raw input.

- [ ] **Step 4: Verify phone tests GREEN**

Run: `python -m pytest tests/accounts/test_phone.py -q`

Expected: all parameterized cases pass.

- [ ] **Step 5: Write failing OTP lifecycle tests**

```python
from datetime import timedelta
from django.utils import timezone
from freezegun import freeze_time
from apps.accounts.services import LockedOtp, request_otp, verify_otp
from tests.accounts.fakes import RecordingSmsProvider


@freeze_time("2026-08-30 08:00:00")
def test_otp_expires_after_five_minutes(db):
    provider = RecordingSmsProvider()
    request_otp("13800138000", "203.0.113.1", provider)
    code = provider.last_code
    with freeze_time(timezone.now() + timedelta(minutes=5, seconds=1)):
        with pytest.raises(LockedOtp):
            verify_otp("13800138000", code)


def test_fifth_wrong_attempt_locks_challenge(db):
    provider = RecordingSmsProvider()
    request_otp("13800138000", "203.0.113.1", provider)
    for _ in range(4):
        with pytest.raises(InvalidOtp):
            verify_otp("13800138000", "000000")
    with pytest.raises(LockedOtp):
        verify_otp("13800138000", "000000")
```

- [ ] **Step 6: Verify OTP tests RED**

Run: `python -m pytest tests/accounts/test_otp.py -q`

Expected: FAIL because challenge behavior is missing.

- [ ] **Step 7: Implement OTP hashing, expiry, counters and provider injection**

Store phone as normalized encrypted text plus deterministic HMAC lookup hash. Store only a salted password hash of the code. Use database transactions and row locks when verifying. A successful first verification creates the account; later verifications reuse it.

- [ ] **Step 8: Add failing throttle tests and implement the exact limits**

Test 60-second resend, five per hour, fifteen per day per phone, and thirty per hour per IP. The implementation must use the cache for fast rejection and persisted challenge rows for audit-safe enforcement.

- [ ] **Step 9: Verify account suite GREEN**

Run: `python -m pytest tests/accounts -q`

Expected: all account, expiry, lockout, reuse and throttle tests pass.

### Task 4: Login pages, session lifecycle, and return path

**Files:**
- Create: `apps/accounts/forms.py`
- Create: `apps/accounts/views.py`
- Create: `apps/accounts/urls.py`
- Create: `apps/accounts/session.py`
- Create: `templates/base_public.html`
- Create: `templates/accounts/login.html`
- Create: `static/css/tokens.css`
- Create: `static/css/public.css`
- Create: `static/js/login.js`
- Create: `tests/accounts/test_login_views.py`
- Create: `tests/accounts/test_session.py`
- Modify: `config/urls.py`

**Interfaces:**
- Consumes: `request_otp` and `verify_otp` from Task 3.
- Produces: `/login/`, `/login/request-code/`, `/login/verify/`, `/logout/`, and safe `next` path restoration.

- [ ] **Step 1: Write failing view tests**

```python
def test_login_page_contains_required_controls(client):
    response = client.get("/login/")
    assert response.status_code == 200
    for text in ["手机号", "验证码", "获取验证码", "登录", "隐私政策"]:
        assert text in response.content.decode()


def test_external_next_url_is_not_followed(client, verified_account):
    response = client.post(
        "/login/verify/?next=https://evil.example/",
        {"phone": "13800138000", "code": verified_account.code},
    )
    assert response.headers["Location"].startswith("/")
```

- [ ] **Step 2: Verify view tests RED**

Run: `python -m pytest tests/accounts/test_login_views.py -q`

Expected: FAIL with 404/missing URL.

- [ ] **Step 3: Implement accessible P00 and POST-only actions**

Use CSRF-protected forms, generic error copy, safe local return paths and 60-second client countdown that never replaces server throttling. Do not expose whether a phone already has an account.

- [ ] **Step 4: Write and pass session expiry tests**

Store `session_started_at` and `session_last_seen_at`. Middleware expires sessions at 24 idle hours or seven absolute days; logout flushes the session. Verify both boundary times with frozen time tests.

- [ ] **Step 5: Verify login flow GREEN**

Run: `python -m pytest tests/accounts/test_login_views.py tests/accounts/test_session.py -q`

Expected: all login, CSRF, redirect and expiry tests pass.

### Task 5: Versioned consent and one-patient onboarding

**Files:**
- Create: `apps/patients/__init__.py`
- Create: `apps/patients/apps.py`
- Create: `apps/patients/models.py`
- Create: `apps/patients/services.py`
- Create: `apps/patients/forms.py`
- Create: `apps/patients/views.py`
- Create: `apps/patients/urls.py`
- Create: `apps/patients/admin.py`
- Create: `apps/patients/migrations/0001_initial.py`
- Modify: `apps/accounts/models.py`
- Create: `apps/accounts/migrations/0003_consentrecord.py`
- Create: `templates/patients/onboarding.html`
- Create: `templates/patients/home_placeholder.html`
- Create: `tests/patients/test_onboarding_service.py`
- Create: `tests/patients/test_onboarding_views.py`
- Modify: `config/urls.py`

**Interfaces:**
- Consumes: an authenticated `Account`, display name, and three explicit consent booleans.
- Produces: `create_patient_space(account, display_name, consents, request_meta) -> Patient` and P01 routes.

- [ ] **Step 1: Write failing service tests**

```python
import pytest
from apps.patients.services import MissingConsent, create_patient_space


def test_patient_name_is_trimmed_and_all_consents_are_recorded(db, account):
    patient = create_patient_space(
        account,
        "  妈妈  ",
        {"privacy": True, "sensitive": True, "upload_authority": True},
        {"ip": "203.0.113.1"},
    )
    assert patient.display_name == "妈妈"
    assert account.consent_records.filter(withdrawn_at__isnull=True).count() == 3


def test_missing_any_consent_rejects_creation(db, account):
    with pytest.raises(MissingConsent):
        create_patient_space(
            account,
            "妈妈",
            {"privacy": True, "sensitive": False, "upload_authority": True},
            {"ip": "203.0.113.1"},
        )
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/patients/test_onboarding_service.py -q`

Expected: FAIL because patient and consent models do not exist.

- [ ] **Step 3: Implement atomic creation and database uniqueness**

The service validates 1–20 visible characters after trimming, creates three `ConsentRecord` rows with exact policy versions from settings, and creates one `Patient` in a single transaction. A database unique constraint on `account_id` is mandatory; a second attempt returns the existing patient without duplicating consent.

- [ ] **Step 4: Write failing P01 routing tests**

Verify first login redirects to `/onboarding/`, an onboarded user goes to `/`, unauthenticated users go to P00 with a safe return path, and updated mandatory consent versions redirect an existing user to a concise re-consent page.

- [ ] **Step 5: Implement and verify P01 GREEN**

Run: `python -m pytest tests/patients -q`

Expected: all validation, idempotency, consent-version and routing tests pass.

### Task 6: Authenticated shell, tenant resolver, and initial security matrix

**Files:**
- Create: `apps/core/tenant.py`
- Create: `apps/core/decorators.py`
- Create: `templates/base_app.html`
- Create: `static/css/app-shell.css`
- Create: `static/js/app-shell.js`
- Create: `tests/security/test_tenant_isolation.py`
- Create: `tests/accessibility/test_shell_markup.py`
- Modify: `apps/patients/views.py`
- Modify: `config/urls.py`

**Interfaces:**
- Consumes: authenticated request and patient-scoped UUID.
- Produces: `get_request_patient(request) -> Patient` and `get_patient_object_or_404(queryset, request, id)` that always filters in SQL.

- [ ] **Step 1: Write failing tenant-isolation tests**

```python
def test_account_cannot_resolve_another_accounts_patient(rf, account_a, patient_b):
    request = rf.get("/")
    request.user = account_a
    with pytest.raises(Http404):
        get_patient_object_or_404(Patient.objects.all(), request, patient_b.id)
```

- [ ] **Step 2: Verify RED, implement SQL-scoped resolution, and verify GREEN**

Run: `python -m pytest tests/security/test_tenant_isolation.py -q`

Expected before implementation: import failure. Expected after implementation: pass, with query inspection proving `account_id` is part of the SQL filter.

- [ ] **Step 3: Implement the P02 shell placeholder**

Render fixed left navigation entries “首页”“病案”“我的”, patient display name, task status entry, skip link, visible keyboard focus, no horizontal overflow at 1280×720, and a single disabled “上传资料” placeholder that the upload plan will activate.

- [ ] **Step 4: Verify accessibility structure**

Run: `python -m pytest tests/accessibility/test_shell_markup.py -q`

Expected: landmarks, labels, focusable navigation and minimum copy requirements pass.

### Task 7: Development services, migrations, and acceptance evidence

**Files:**
- Create: `compose.yaml`
- Create: `deploy/docker/postgres/init.sql`
- Create: `deploy/README.md`
- Create: `tests/acceptance/test_ac00_ac01.py`
- Create: `docs/verification/README.md`
- Create: `docs/verification/ac00-ac01.md`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: local PostgreSQL, Redis and MinIO services plus repeatable AC-00/AC-01 verification.

- [ ] **Step 1: Write failing AC-00/AC-01 tests**

Cover successful OTP login, expired login with safe path return, first-time onboarding, absence of other mandatory medical fields, three consent records, and second-patient rejection.

- [ ] **Step 2: Verify acceptance tests RED for the missing integrated behavior**

Run: `python -m pytest tests/acceptance/test_ac00_ac01.py -q`

Expected: at least one assertion fails before final wiring.

- [ ] **Step 3: Add local service definitions and PostgreSQL extension initialization**

`compose.yaml` exposes services only on loopback, uses named volumes, health checks, non-default development passwords from `.env`, and initializes `pg_trgm`. MinIO buckets remain private.

- [ ] **Step 4: Apply migrations and run the integrated suite**

Run: `python manage.py migrate --noinput`

Run: `python manage.py check --deploy --settings=config.settings.test`

Run: `python -m pytest -q`

Expected: all tests pass; deploy checks may emit only explicitly documented test-environment warnings, never `phr.E001`–`phr.E003`.

- [ ] **Step 5: Record verification evidence**

`docs/verification/ac00-ac01.md` records the exact commands, UTC timestamp, test counts, Python/Django versions, and any browser checks performed. It must not contain phone numbers or patient names.

## Plan self-review

- Each task has an independently testable deliverable.
- The interfaces used by later tasks are introduced by earlier tasks with stable names.
- P00, P01, session expiry, return path, consent versioning and tenant foundations are covered.
- Upload, parsing, search, viewer, trends, deletion and production release remain in subsequent plans; this plan does not pretend those PRD areas are complete.
- No implementation step depends on a cloud account or production SMS credential.
