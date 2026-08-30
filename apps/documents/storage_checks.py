import json

from botocore.exceptions import ClientError


class PrivateStorageCheckFailed(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _client_error_code(error):
    return str(error.response.get("Error", {}).get("Code", "")) if isinstance(error, ClientError) else ""


def _missing_configuration(error):
    return _client_error_code(error) in {
        "NoSuchBucketPolicy",
        "NoSuchPublicAccessBlockConfiguration",
        "NotImplemented",
        "501",
    }


def _statements(policy):
    statements = policy.get("Statement", []) if isinstance(policy, dict) else []
    if isinstance(statements, dict):
        statements = [statements]
    return [statement for statement in statements if isinstance(statement, dict)]


def _contains_public_principal(value):
    if value == "*":
        return True
    if isinstance(value, dict):
        return any(_contains_public_principal(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_public_principal(item) for item in value)
    return False


def _has_public_allow(policy):
    for statement in _statements(policy):
        principal = statement.get("Principal")
        if statement.get("Effect") == "Allow" and (
            "NotPrincipal" in statement or _contains_public_principal(principal)
        ):
            return True
    return False


def verify_private_s3_bucket(
    client,
    bucket,
    *,
    require_encryption=True,
    allow_missing_public_access_block=False,
):
    try:
        acl = client.get_bucket_acl(Bucket=bucket)
    except Exception:
        raise PrivateStorageCheckFailed("bucket_acl_unavailable") from None
    for grant in acl.get("Grants", []):
        grantee = grant.get("Grantee", {})
        if grantee.get("Type") != "CanonicalUser" or grantee.get("URI"):
            raise PrivateStorageCheckFailed("bucket_acl_public")

    try:
        response = client.get_public_access_block(Bucket=bucket)
    except ClientError as error:
        if not allow_missing_public_access_block or not _missing_configuration(error):
            raise PrivateStorageCheckFailed("public_access_block_unavailable") from None
    except Exception:
        raise PrivateStorageCheckFailed("public_access_block_unavailable") from None
    else:
        block = response.get("PublicAccessBlockConfiguration", {})
        required = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
        if not all(block.get(field) is True for field in required):
            raise PrivateStorageCheckFailed("public_access_block_incomplete")

    try:
        policy_response = client.get_bucket_policy(Bucket=bucket)
    except ClientError as error:
        if not _missing_configuration(error):
            raise PrivateStorageCheckFailed("bucket_policy_unavailable") from None
    except Exception:
        raise PrivateStorageCheckFailed("bucket_policy_unavailable") from None
    else:
        try:
            policy = json.loads(policy_response.get("Policy", "{}"))
        except (TypeError, json.JSONDecodeError):
            raise PrivateStorageCheckFailed("bucket_policy_invalid") from None
        if _has_public_allow(policy):
            raise PrivateStorageCheckFailed("bucket_policy_public")

    try:
        versioning = client.get_bucket_versioning(Bucket=bucket)
    except Exception:
        raise PrivateStorageCheckFailed("bucket_versioning_unavailable") from None
    if versioning.get("Status") != "Enabled":
        raise PrivateStorageCheckFailed("bucket_versioning_disabled")

    if require_encryption:
        try:
            encryption = client.get_bucket_encryption(Bucket=bucket)
        except Exception:
            raise PrivateStorageCheckFailed("bucket_encryption_unavailable") from None
        algorithms = {
            rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
            for rule in encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        }
        if not algorithms & {"AES256", "aws:kms", "aws:kms:dsse"}:
            raise PrivateStorageCheckFailed("bucket_encryption_disabled")
    return True
