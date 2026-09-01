# Task 1 report: purpose-bound OTP challenges and password-attempt throttling

## Implementation summary

- Added purpose and optional account binding to OTP challenges, plus the durable `PasswordAttemptThrottle` model and migration.
- Replaced OTP account creation with purpose/account-bound `request_otp` and exact-challenge `consume_otp`; preserved expiry, five-attempt locking, single-use, resend/send limits, encrypted phone storage, and safe delivery errors.
- Added a 15-minute first-failure-anchored password throttle: five failures per phone hash, thirty per IP hash, generic limit errors, expiry reset, and successful-verification reset.
- Passed OTP purpose through every SMS provider and signed HTTPS payload. Added password-throttle cleanup during account purge and privacy-safe admin views.

## Files changed

- `apps/accounts/models.py`
- `apps/accounts/migrations/0007_authentication_flows.py`
- `apps/accounts/admin.py`
- `apps/accounts/services.py`
- `apps/accounts/providers.py`
- `apps/accounts/deletion.py` (required deletion cleanup for the new model)
- `tests/accounts/fakes.py`
- `tests/accounts/test_otp.py`
- `tests/accounts/test_login_service.py`
- `tests/accounts/test_sms_gateway.py`
- `tests/accounts/test_account_deletion.py`

## RED

Command:

```powershell
python -m pytest tests/accounts/test_otp.py tests/accounts/test_login_service.py -q
```

Output:

```text
ImportError: cannot import name 'consume_otp' from 'apps.accounts.services'
1 error in 0.59s
```

Reason: the new test imported the required exact-challenge consumption API before it existed.

## GREEN

Command:

```powershell
python -m pytest tests/accounts/test_otp.py tests/accounts/test_login_service.py tests/accounts/test_sms_gateway.py tests/accounts/test_account_deletion.py -q
```

Output:

```text
47 passed in 2.60s
```

Additional checks: `python manage.py makemigrations --check --dry-run` returned `No changes detected`; `git diff --check` returned no whitespace errors.

## Full Python suite

Command:

```powershell
python -m pytest -q
```

Output:

```text
21 failed, 651 passed, 3 skipped, 43 warnings in 90.17s
```

All 21 failures are legacy OTP-only login/onboarding/browser tests. They call the deliberately retired OTP-only request/verify boundary: 20 fail because the old view calls `request_otp` without the new required purpose; one directly calls the provider with the old two-argument signature. The views/forms and their migration are explicitly assigned to Tasks 2–4, so they were not changed in this task.

## Self-review findings

- Confirmed no account creation remains in OTP request or consumption.
- Confirmed SIGN_IN and PASSWORD_RESET requests require an active account with the same phone hash, and consumption requires the exact purpose/account tuple.
- Confirmed every provider call receives purpose; no raw phones, codes, or passwords were added to log/error paths.
- Confirmed throttle rows are created and locked in deterministic scope order, use a fifteen-minute first-failure window, and do not identify the locking scope in their exception message.
- Confirmed deletion removes phone-keyed OTP and password-throttle state, including challenges not account-bound.

## Issues / concerns

The repository-wide suite cannot be green until the planned password-first login and first-use flow replace the legacy OTP-only views and acceptance/browser fixtures in Tasks 2–4. This task intentionally leaves that migration out of scope; its focused service/provider/deletion suite is green.
