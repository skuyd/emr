from django.contrib.auth.base_user import BaseUserManager


class AccountManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, phone_hash, phone_encrypted, **extra_fields):
        if not phone_hash:
            raise ValueError("The phone hash must be set")
        if not phone_encrypted:
            raise ValueError("The encrypted phone value must be set")

        password = extra_fields.pop("password", None)
        account = self.model(
            phone_hash=phone_hash,
            phone_encrypted=phone_encrypted,
            **extra_fields,
        )
        if password is None:
            account.set_unusable_password()
        else:
            account.set_password(password)
        account.save(using=self._db)
        return account

    def create_superuser(self, phone_hash, phone_encrypted, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(phone_hash, phone_encrypted, **extra_fields)
