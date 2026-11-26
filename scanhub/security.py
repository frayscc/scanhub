"""Password hashing and opaque session identifiers; no default credentials."""
import hashlib
import hmac
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt:{salt}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        method, salt, expected = encoded.split(":")
        if method != "scrypt" or len(password) > 1024:
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_token(value: str, expected: str) -> bool:
    if not expected:
        return False
    if expected.startswith("sha256:"):
        return hmac.compare_digest(token_digest(value), expected[7:])
    return hmac.compare_digest(value.encode(), expected.encode())


if __name__ == "__main__":
    import getpass
    password = getpass.getpass("管理员密码（至少 12 位）: ")
    if len(password) < 12:
        raise SystemExit("密码至少 12 位")
    print(hash_password(password))
