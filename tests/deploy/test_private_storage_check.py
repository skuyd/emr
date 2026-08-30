import json

from botocore.exceptions import ClientError
import pytest

from apps.documents.storage_checks import PrivateStorageCheckFailed, verify_private_s3_bucket


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "private-detail"}}, "operation")


class PrivateClient:
    def __init__(self):
        self.overrides = {}

    def _result(self, name, default):
        value = self.overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    def get_bucket_acl(self, **_kwargs):
        return self._result(
            "acl",
            {"Grants": [{"Grantee": {"Type": "CanonicalUser"}, "Permission": "FULL_CONTROL"}]},
        )

    def get_public_access_block(self, **_kwargs):
        return self._result(
            "public_block",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            },
        )

    def get_bucket_policy(self, **_kwargs):
        return self._result("policy", client_error("NoSuchBucketPolicy"))

    def get_bucket_versioning(self, **_kwargs):
        return self._result("versioning", {"Status": "Enabled"})

    def get_bucket_encryption(self, **_kwargs):
        return self._result(
            "encryption",
            {
                "ServerSideEncryptionConfiguration": {
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
                }
            },
        )


def test_private_versioned_encrypted_bucket_passes():
    assert verify_private_s3_bucket(PrivateClient(), "opaque-bucket") is True


@pytest.mark.parametrize(
    "field,value,code",
    [
        (
            "acl",
            {"Grants": [{"Grantee": {"Type": "Group", "URI": "AllUsers"}, "Permission": "READ"}]},
            "bucket_acl_public",
        ),
        (
            "public_block",
            {"PublicAccessBlockConfiguration": {"BlockPublicAcls": True}},
            "public_access_block_incomplete",
        ),
        (
            "policy",
            {"Policy": json.dumps({"Statement": {"Effect": "Allow", "Principal": "*"}})},
            "bucket_policy_public",
        ),
        (
            "policy",
            {
                "Policy": json.dumps(
                    {"Statement": {"Effect": "Allow", "Principal": {"AWS": ["private-role", "*"]}}}
                )
            },
            "bucket_policy_public",
        ),
        (
            "policy",
            {
                "Policy": json.dumps(
                    {"Statement": {"Effect": "Allow", "NotPrincipal": {"AWS": "excluded-role"}}}
                )
            },
            "bucket_policy_public",
        ),
        ("versioning", {"Status": "Suspended"}, "bucket_versioning_disabled"),
        (
            "encryption",
            {"ServerSideEncryptionConfiguration": {"Rules": []}},
            "bucket_encryption_disabled",
        ),
    ],
)
def test_public_unversioned_or_unencrypted_bucket_fails_closed(field, value, code):
    client = PrivateClient()
    client.overrides[field] = value

    with pytest.raises(PrivateStorageCheckFailed) as raised:
        verify_private_s3_bucket(client, "opaque-bucket")

    assert raised.value.code == code
    assert "private-detail" not in str(raised.value)


def test_closed_trial_can_allow_missing_public_block_and_encryption_but_not_public_policy():
    client = PrivateClient()
    client.overrides["public_block"] = client_error("NotImplemented")

    assert verify_private_s3_bucket(
        client,
        "opaque-bucket",
        require_encryption=False,
        allow_missing_public_access_block=True,
    ) is True

    client.overrides["policy"] = {
        "Policy": json.dumps({"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}}]})
    }
    with pytest.raises(PrivateStorageCheckFailed):
        verify_private_s3_bucket(
            client,
            "opaque-bucket",
            require_encryption=False,
            allow_missing_public_access_block=True,
        )
