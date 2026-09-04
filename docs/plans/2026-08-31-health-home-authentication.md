# Health Home Two-Factor Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace OTP-only account creation with privacy-safe password-plus-SMS authentication, verified first use, password recovery, and an idempotent development account.

**Architecture:** Keep phone normalization, encryption, OTP generation, and throttling in focused account-domain services. OTP verification proves control of a phone but never creates an account by itself; short-lived server-side flow state carries only account/challenge identifiers, a safe local destination, and timestamps. Django password hashing, validators, login/session rotation, and the existing registered-session table provide the password and revocation boundaries.

**Tech Stack:** Python 3.11, Django 5.2, Django templates/forms/sessions, pytest, pytest-django, Playwright, vanilla JavaScript

**Spec:** `docs/specs/2026-08-31-health-home-warm-ui-auth-design.md`

## Global Constraints

- Login succeeds only after a correct password and a correct six-digit SMS OTP.
- OTPs remain valid for five minutes, single-use, locked after five failed attempts, and subject to the existing phone/IP send limits.
- Invalid phone, missing/disabled account, and wrong password return exactly `手机号或密码不正确` and never send an OTP.
- `pending_mfa` stores only account ID, challenge ID, safe `next`, and issue time; it stores no phone number or password.
- First use creates or upgrades an account only after phone verification; requesting an OTP never creates an account.
- Existing unusable-password accounts are upgraded in place without changing patient, consent, document, or ownership rows.
- Password reset always gives an account-neutral response and revokes every registered and legacy authenticated session after success.
- All enrollment and reset passwords run Django password validators; the development fixture password is the only local-only exception.
- Raw phone numbers, passwords, OTPs, and fixed development credentials never enter logs, analytics, errors, or command output.
- The fixed code is `230412` only when `DEBUG=True` and `OTP_PROVIDER="development"`; production keeps `OTP_FIXED_CODE=None` and fails checks otherwise.
- Preserve all existing safe-`next`, CSRF, encrypted-phone, privacy-filter, session-expiry, consent, and account-deletion behavior.

---

### Task 1: Purpose-bound OTP challenges and password-attempt throttling

**Files:**
- Modify: `apps/accounts/models.py`
- Create: `apps/accounts/migrations/0007_authentication_flows.py`
- Modify: `apps/accounts/admin.py`
- Modify: `apps/accounts/services.py`
- Modify: `apps/accounts/providers.py`
- Modify: `tests/accounts/fakes.py`
- Modify: `tests/accounts/test_otp.py`
- Modify: `tests/accounts/test_login_service.py`
- Modify: `tests/accounts/test_sms_gateway.py`
- Modify: `tests/accounts/test_account_deletion.py`

**Interfaces:**
- Consumes: normalized phone input, requester IP, OTP purpose, optional account, and an injected `SmsProvider`.
- Produces: `request_otp(phone, ip, provider, *, purpose, account=None) -> OtpChallenge`, `consume_otp(challenge_id, code, *, purpose, account_id=None) -> OtpChallenge`, and `enforce_password_attempt_limits(phone_hash, ip_hash, *, succeeded=False) -> None`.

- [ ] **Step 1: Write failing purpose-isolation and no-auto-create tests**

```python
def test_consuming_first_use_otp_does_not_create_account(db):
    provider = RecordingSmsProvider()
    challenge = request_otp(
        "13800138000", "203.0.113.1", provider,
        purpose=OtpChallenge.Purpose.FIRST_USE,
    )
    consumed = consume_otp(
        challenge.pk, provider.last_code,
        purpose=OtpChallenge.Purpose.FIRST_USE,
    )
    assert consumed.consumed_at is not None
    assert Account.objects.count() == 0


def test_otp_cannot_cross_authentication_purposes(db):
    provider = RecordingSmsProvider()
    challenge = request_otp(
        "13800138000", "203.0.113.1", provider,
        purpose=OtpChallenge.Purpose.FIRST_USE,
    )
    with pytest.raises(LockedOtp):
        consume_otp(
            challenge.pk, provider.last_code,
            purpose=OtpChallenge.Purpose.SIGN_IN,
        )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/accounts/test_otp.py tests/accounts/test_login_service.py -q`

Expected: FAIL because challenges have no purpose and `consume_otp` does not exist.

- [ ] **Step 3: Add purpose/account fields and a durable password throttle**

`OtpChallenge` gains these exact fields and choices:

```python
class Purpose(models.TextChoices):
    SIGN_IN = "sign_in", "Sign in"
    FIRST_USE = "first_use", "First use"
    PASSWORD_RESET = "password_reset", "Password reset"

purpose = models.CharField(max_length=20, choices=Purpose.choices)
account = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    null=True,
    blank=True,
    on_delete=models.CASCADE,
    related_name="otp_challenges",
)
```

Add `PasswordAttemptThrottle(scope, identifier_hash, window_started_at, attempts)` with a unique `(scope, identifier_hash)` constraint. The service locks its phone and IP rows, allows at most five failures per phone hash and thirty failures per IP in a rolling fifteen-minute window, resets a row on successful password verification, and raises `ThrottledPassword` without exposing which limit fired.

- [ ] **Step 4: Refactor OTP request/consume without weakening existing limits**

`request_otp` passes the exact purpose to `provider.send_otp(normalized_phone, code, purpose)` and validates that sign-in/reset challenges have the matching active account. `consume_otp` selects the exact challenge row for update, checks purpose/account/expiry/lock/consumption, increments attempts, and consumes once. Remove account creation from OTP verification and update deletion cleanup for both new models.

- [ ] **Step 5: Update provider contracts and verify GREEN**

```python
class SmsProvider(Protocol):
    def send_otp(self, phone: str, code: str, purpose: str) -> None: ...
```

The HTTPS body contains the supplied `purpose` and the development/fake providers accept the third argument without logging it. Run:

`python -m pytest tests/accounts/test_otp.py tests/accounts/test_login_service.py tests/accounts/test_sms_gateway.py tests/accounts/test_account_deletion.py -q`

Expected: all purpose, expiry, lockout, replay, throttle, provider, and deletion tests pass.

### Task 2: Password verification and bounded server-side flow state

**Files:**
- Create: `apps/accounts/authentication.py`
- Create: `apps/accounts/flow_state.py`
- Create: `tests/accounts/test_authentication.py`
- Create: `tests/accounts/test_flow_state.py`

**Interfaces:**
- Consumes: raw phone/password/IP/provider and Django request sessions.
- Produces: `begin_password_login(phone, password, ip, provider) -> PendingMfa`, `complete_password_login(challenge_id, code, account_id) -> Account`, `store_pending_mfa(request, pending, destination)`, and `load_pending_mfa(request) -> PendingMfaState | None`.

- [ ] **Step 1: Write failing password-boundary tests**

```python
def test_wrong_password_does_not_issue_otp_or_authenticate(db, account_with_password):
    provider = RecordingSmsProvider()
    with pytest.raises(InvalidCredentials, match="Invalid credentials"):
        begin_password_login("13800138000", "wrong", "203.0.113.1", provider)
    assert provider.messages == []
    assert OtpChallenge.objects.count() == 0


def test_correct_password_requires_matching_otp(db, account_with_password):
    provider = RecordingSmsProvider()
    pending = begin_password_login(
        "13800138000", "valid-password", "203.0.113.1", provider
    )
    assert pending.account_id == account_with_password.pk
    with pytest.raises(InvalidOtp):
        complete_password_login(pending.challenge_id, "000000", pending.account_id)
    assert complete_password_login(
        pending.challenge_id, provider.last_code, pending.account_id
    ) == account_with_password
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accounts/test_authentication.py tests/accounts/test_flow_state.py -q`

Expected: FAIL because the authentication and flow-state modules do not exist.

- [ ] **Step 3: Implement password-first login and privacy-safe failure behavior**

Normalize and hash the phone, query by `phone_hash`, and call `check_password`. Missing, inactive, unusable-password, and wrong-password accounts all raise `InvalidCredentials`; only a valid active account calls `request_otp(..., purpose=SIGN_IN, account=account)`. Apply the Task 1 phone/IP throttle before returning and clear its phone row on success. No exception message contains input data.

- [ ] **Step 4: Implement exact session payload validation**

```python
@dataclass(frozen=True)
class PendingMfaState:
    account_id: uuid.UUID
    challenge_id: int
    destination: str
    issued_at: int
```

`store_pending_mfa` writes only those four serialized values under `pending_mfa`. `load_pending_mfa` rejects malformed values and values aged 300 seconds or more, clears rejected state, and never trusts a destination that fails `_safe_next`. Enrollment and reset use separate keys so a challenge cannot be promoted across flows.

- [ ] **Step 5: Verify GREEN and inspect session serialization**

Run: `python -m pytest tests/accounts/test_authentication.py tests/accounts/test_flow_state.py -q`

Expected: tests pass and decoded session dictionaries contain no phone, password, or OTP values.

### Task 3: Two-step login views, forms, and session rotation

**Files:**
- Modify: `apps/accounts/forms.py`
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/urls.py`
- Modify: `templates/accounts/login.html`
- Create: `templates/accounts/login_mfa.html`
- Modify: `static/js/login.js`
- Replace assertions in: `tests/accounts/test_login_views.py`
- Modify: `tests/accounts/test_session.py`

**Interfaces:**
- Consumes: Task 2 authentication and flow-state functions, `_safe_next`, Django `login`, and onboarding routing.
- Produces: GET `/login/`, POST `/login/password/`, and GET/POST `/login/verify/`.

- [ ] **Step 1: Write failing two-step view tests**

```python
def test_password_step_uses_one_generic_invalid_credentials_message(client, account):
    missing = client.post("/login/password/", {"phone": "13900000000", "password": "wrong"})
    wrong = client.post("/login/password/", {"phone": "13800138000", "password": "wrong"})
    assert missing.status_code == wrong.status_code == 400
    assert "手机号或密码不正确" in missing.content.decode()
    assert "手机号或密码不正确" in wrong.content.decode()
    assert OtpChallenge.objects.count() == 0


def test_mfa_login_rotates_session_and_honors_safe_next(client, account, provider):
    before = client.session.session_key
    client.post("/login/password/", {
        "phone": "13800138000", "password": "valid-password", "next": "/records/"
    })
    response = client.post("/login/verify/", {"code": provider.last_code})
    assert response["Location"] == "/records/"
    assert client.session.session_key != before
    assert "pending_mfa" not in client.session
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accounts/test_login_views.py tests/accounts/test_session.py -q`

Expected: FAIL because the password route and MFA page do not exist.

- [ ] **Step 3: Add accessible forms and login routes**

`PasswordLoginForm` exposes persistent `手机号` and `密码` labels with `autocomplete="tel"` and `autocomplete="current-password"`; `MfaForm` exposes a six-digit `验证码` field with `autocomplete="one-time-code"`. Password POST returns HTTP 400 for validation/credential failure, stores `pending_mfa` only after an OTP is accepted by the provider, and redirects to `/login/verify/`. The verify view returns 400 on invalid OTP, 400 on missing/expired state, and never authenticates early.

- [ ] **Step 4: Complete login only after OTP and preserve onboarding routing**

After `complete_password_login`, call Django `login(..., backend="django.contrib.auth.backends.ModelBackend")`, initialize session timestamps, record the existing privacy-safe `login_succeeded` event, clear all authentication flow keys, and route to onboarding, safe `next`, or `/`. Keep logout POST-only with `Clear-Site-Data`.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/accounts/test_login_views.py tests/accounts/test_session.py tests/patients/test_onboarding_views.py -q`

Expected: correct password plus correct OTP logs in; every missing/expired/wrong/locked/replayed branch remains anonymous.

### Task 4: Verified first-use and legacy-account upgrade

**Files:**
- Modify: `apps/accounts/forms.py`
- Modify: `apps/accounts/authentication.py`
- Modify: `apps/accounts/flow_state.py`
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/urls.py`
- Create: `templates/accounts/first_use_phone.html`
- Create: `templates/accounts/first_use_verify.html`
- Create: `templates/accounts/set_password.html`
- Create: `tests/accounts/test_first_use.py`
- Modify: `tests/patients/test_onboarding_views.py`

**Interfaces:**
- Consumes: purpose-bound OTP, `validate_password`, existing encrypted phone/account lookup, and patient onboarding.
- Produces: `/login/first-use/`, `/login/first-use/verify/`, `/login/first-use/password/`, and `create_or_upgrade_account(challenge, password) -> Account`.

- [ ] **Step 1: Write failing creation and upgrade tests**

```python
def test_requesting_first_use_code_never_creates_account(client, provider):
    response = client.post("/login/first-use/", {"phone": "13800138000"})
    assert response.status_code == 302
    assert Account.objects.count() == 0


def test_verified_legacy_account_is_upgraded_in_place(db, client, legacy_account, patient, provider):
    original_patient_id = patient.pk
    complete_first_use_flow(client, "13800138000", provider.last_code, "Strong passphrase 2026")
    legacy_account.refresh_from_db()
    assert legacy_account.has_usable_password()
    assert legacy_account.patient.pk == original_patient_id
    assert Account.objects.count() == 1
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accounts/test_first_use.py -q`

Expected: FAIL with missing first-use URLs.

- [ ] **Step 3: Implement phone, OTP, and password steps with separate state**

The phone step requests `FIRST_USE` OTP and stores only challenge ID/issue time/safe `next`. The verify step consumes that challenge and stores `verified_phone={challenge_id, verified_at}` for at most five minutes. The password step runs `validate_password`, compares confirmation, then atomically creates a new account from the challenge hashes/encrypted phone or calls `set_password` on the one matching unusable-password account. A matching account with an already usable password is not overwritten and receives authenticated guidance to log in or reset instead.

- [ ] **Step 4: Log in the new/upgraded account and retain data ownership**

After creation/upgrade, rotate/login/initialize the session and route to existing onboarding when no patient exists or to safe `next`/home when it does. Assert existing consent/document foreign keys and patient ID remain unchanged.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/accounts/test_first_use.py tests/patients/test_onboarding_views.py tests/security/test_tenant_isolation.py -q`

Expected: new accounts are created only after OTP; legacy accounts upgrade in place with tenant ownership intact.

### Task 5: Account-neutral password reset and complete session revocation

**Files:**
- Modify: `apps/accounts/forms.py`
- Modify: `apps/accounts/authentication.py`
- Modify: `apps/accounts/flow_state.py`
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/urls.py`
- Create: `templates/accounts/forgot_password.html`
- Create: `templates/accounts/reset_verify.html`
- Create: `templates/accounts/reset_password.html`
- Create: `tests/accounts/test_password_reset.py`
- Modify: `tests/accounts/test_session.py`

**Interfaces:**
- Consumes: `revoke_account_sessions(account_id)`, purpose-bound OTP, and Django validators.
- Produces: `/login/forgot-password/`, `/login/forgot-password/verify/`, `/login/forgot-password/new-password/`, and `reset_account_password(account, password) -> None`.

- [ ] **Step 1: Write failing neutrality and revocation tests**

```python
def test_reset_request_is_account_neutral(client, active_account, provider):
    existing = client.post("/login/forgot-password/", {"phone": "13800138000"})
    missing = client.post("/login/forgot-password/", {"phone": "13900000000"})
    assert existing.status_code == missing.status_code == 200
    assert existing.context["status"] == missing.context["status"]


def test_reset_revokes_all_old_sessions(client, active_account, two_authenticated_sessions, provider):
    complete_password_reset(client, "13800138000", provider.last_code, "New strong passphrase 2026")
    assert AccountSession.objects.filter(account=active_account).count() == 0
    assert Session.objects.filter(session_key__in=two_authenticated_sessions).count() == 0
    assert client.get("/").status_code == 302
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accounts/test_password_reset.py tests/accounts/test_session.py -q`

Expected: FAIL with missing reset routes.

- [ ] **Step 3: Implement neutral request and verified reset state**

Always render `如果该手机号可用，我们已发送验证码，请按页面提示继续。` without reflecting the phone. Only active matching accounts receive `PASSWORD_RESET` challenges; missing/inactive accounts get no challenge. OTP verification stores only account/challenge IDs and verification time. The new-password step requires matching confirmation and all Django validators.

- [ ] **Step 4: Save, revoke, and require fresh MFA**

Inside a transaction call `account.set_password`, save, and revoke registered plus decoded legacy sessions. Clear reset state and redirect to `/login/?password-reset=complete`; do not log the user in. A consumed challenge or reused reset ticket fails.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/accounts/test_password_reset.py tests/accounts/test_session.py -q`

Expected: generic responses match, old sessions fail, and the new password still requires login OTP.

### Task 6: Development provider, fixed code, and idempotent fixture account

**Files:**
- Modify: `config/settings/dev.py`
- Modify: `config/settings/production.py`
- Modify: `config/settings/test.py`
- Modify: `.env.example`
- Modify: `apps/accounts/otp.py`
- Modify: `apps/accounts/providers.py`
- Modify: `apps/core/checks.py`
- Create: `apps/accounts/management/__init__.py`
- Create: `apps/accounts/management/commands/__init__.py`
- Create: `apps/accounts/management/commands/seed_development_account.py`
- Create: `tests/accounts/test_development_account.py`
- Modify: `tests/core/test_checks.py`
- Modify: `docs/deployment/local-development.md`

**Interfaces:**
- Consumes: dev settings, existing phone crypto, and `Account.set_password()`.
- Produces: `python manage.py seed_development_account` and local credentials defined by the spec.

- [ ] **Step 1: Write failing fixed-code and idempotency tests**

```python
@override_settings(DEBUG=True, OTP_PROVIDER="development", OTP_FIXED_CODE="230412")
def test_development_provider_uses_spec_code():
    assert generate_code() == "230412"


def test_seed_is_idempotent_and_preserves_patient_data(settings, django_user_model):
    settings.DEBUG = True
    settings.OTP_PROVIDER = "development"
    call_command("seed_development_account")
    account = django_user_model.objects.get(phone_hash=hash_phone("+8618000000000"))
    patient = Patient.objects.create(account=account, display_name="已有档案")
    call_command("seed_development_account")
    assert django_user_model.objects.count() == 1
    assert account.pk == django_user_model.objects.get().pk
    assert Patient.objects.get().pk == patient.pk
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accounts/test_development_account.py tests/core/test_checks.py -q`

Expected: FAIL because the provider name/code and seed command are absent.

- [ ] **Step 3: Enforce exact environment boundary**

Set dev defaults to `OTP_PROVIDER="development"` and `OTP_FIXED_CODE="230412"`; keep production/test fixed code `None`. `generate_code` may read a fixed code only when both `DEBUG is True` and provider is `development`. Security checks reject any non-DEBUG fixed code and reject the development provider outside DEBUG.

- [ ] **Step 4: Add the idempotent seed command without exposing credentials**

The command refuses to run unless DEBUG/development-provider are both active, normalizes/encrypts/hashes `18000000000`, uses `get_or_create(phone_hash=...)`, updates encrypted phone only when needed, calls `set_password("123321")`, and prints only `Development account is ready.` It never creates or edits Patient, ConsentRecord, Document, or other ownership data.

- [ ] **Step 5: Document and verify the local setup**

Document this sequence: `python manage.py migrate`, `python manage.py seed_development_account`, `python manage.py runserver`. Run:

`python -m pytest tests/accounts/test_development_account.py tests/core/test_checks.py tests/test_project_configuration.py -q`

Expected: all tests pass and captured logs/output contain none of the phone, password, or OTP strings.

### Task 7: Authentication regression and browser journeys

**Files:**
- Modify: `tests/acceptance/test_ac00_ac01.py`
- Modify: `tests/browser/test_ac00_ac01_browser.py`
- Modify: `tests/browser/test_ac02_upload_browser.py`
- Modify: `tests/e2e/phr-v1.spec.ts`
- Modify: `docs/verification/ac00-ac01.md`

**Interfaces:**
- Consumes: Tasks 1–6 and existing onboarding/upload flows.
- Produces: integrated first-use, MFA login, reset/revocation, and safe-return evidence.

- [ ] **Step 1: Replace OTP-only fixtures with explicit first-use and MFA helpers**

```python
def first_use(page, base_url, phone, code, password):
    page.goto(f"{base_url}/login/first-use/")
    page.locator("#id_phone").fill(phone)
    page.get_by_role("button", name="发送验证码").click()
    page.locator("#id_code").fill(code)
    page.get_by_role("button", name="验证手机号").click()
    page.locator("#id_password1").fill(password)
    page.locator("#id_password2").fill(password)
    page.get_by_role("button", name="设置密码").click()
```

- [ ] **Step 2: Add integrated browser assertions**

Cover first-use creation, legacy upgrade, password rejection without OTP, successful password plus OTP login, expired/missing `pending_mfa`, safe `next`, reset neutrality, session revocation, labels/autocomplete, keyboard password visibility control, 390/768/1440 overflow, and no unexpected console/4xx/5xx events.

- [ ] **Step 3: Run authentication and dependent regressions**

Run: `python -m pytest tests/accounts tests/patients tests/security tests/acceptance/test_ac00_ac01.py -q`

Run: `python -m pytest tests/browser/test_ac00_ac01_browser.py tests/browser/test_ac02_upload_browser.py -q`

Expected: all tests pass; browser tests may skip only when no supported local browser exists and the skip reason is explicit.

- [ ] **Step 4: Run project checks and migration consistency**

Run: `python manage.py makemigrations --check --dry-run`

Run: `python manage.py check`

Expected: no model drift and no Django system-check errors.

## Plan self-review

- Spec sections 9–11 and authentication items in section 12 map to Tasks 1–7.
- OTP purpose, account creation, password hashing, generic errors, safe `next`, throttling, session rotation, legacy upgrade, reset revocation, fixed-code production checks, and dev fixture idempotency each have direct tests.
- No task stores or logs raw credentials, and no browser fixture can bypass MFA for a normal sign-in.
- The only dependency exposed to the UI plan is the stable set of server-rendered auth routes, form field IDs, context statuses, and error-summary semantics.
