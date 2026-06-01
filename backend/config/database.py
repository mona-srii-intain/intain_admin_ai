import traceback
import yaml
import logging
import os
import json
import base64
import re
import urllib.request
import urllib.error
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_fixed
import snowflake.connector
from snowflake.connector.errors import DatabaseError
import asyncio
from cryptography.hazmat.primitives import serialization

global_pool = None
_active_pool_conn_key = None

logger = logging.getLogger("uvicorn.error")

_env_loaded = False
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _cfg_str(value) -> str:
    """Normalize config/env values into trimmed strings."""
    if value is None:
        return ""
    return str(value).strip().strip('"').strip("'")


def _load_env_from_backend_dir_once() -> None:
    """
    Load backend/.env into process environment if present.
    This makes ROLE_ID/SECRET_ID available when app is launched without exported env vars.
    """
    global _env_loaded
    if _env_loaded:
        return

    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        _env_loaded = True
        return

    try:
        with env_path.open("r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    finally:
        _env_loaded = True

def _http_json(url, *, method="GET", headers=None, payload=None, timeout_seconds=15):
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in req_headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} calling {url}: {body or e.reason}") from e


def _vault_login_approle(vault_addr: str, role_id: str, secret_id: str) -> str:
    login_url = vault_addr.rstrip("/") + "/v1/auth/approle/login"
    logger.info("Vault: logging in via AppRole")
    resp = _http_json(
        login_url,
        method="POST",
        payload={"role_id": role_id, "secret_id": secret_id},
    )
    token = (resp.get("auth") or {}).get("client_token")
    if not token:
        raise RuntimeError("Vault AppRole login failed: missing client_token in response")
    logger.info("Vault: AppRole login successful")
    return token


def _vault_read_kv(vault_addr: str, token: str, path: str) -> dict:
    # Accept either "/v1/..." or "Snowflake/data/Credentials" style.
    if path.startswith("/v1/"):
        url = vault_addr.rstrip("/") + path
    else:
        url = vault_addr.rstrip("/") + "/v1/" + path.lstrip("/")

    logger.info("Vault: reading secret from configured path")
    resp = _http_json(url, headers={"X-Vault-Token": token})
    # KV v2 format: {"data": {"data": {...}, "metadata": {...}}}
    data = resp.get("data") or {}
    if isinstance(data, dict) and "data" in data and isinstance(data.get("data"), dict):
        return data.get("data") or {}
    return data if isinstance(data, dict) else {}


def _looks_like_base64(s: str) -> bool:
    s2 = "".join(s.split())
    if len(s2) < 16 or len(s2) % 4 != 0:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return all(c in allowed for c in s2)


def _private_key_bytes_to_snowflake_der(key_bytes: bytes) -> bytes:
    """
    Snowflake connector expects a PKCS8 DER private key bytes (not PEM text).
    We accept either PEM or DER and always return DER.
    """
    try:
        p_key = serialization.load_pem_private_key(key_bytes, password=None)
    except Exception:
        p_key = serialization.load_der_private_key(key_bytes, password=None)

    return p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_private_key_from_file(private_key_file_path: str) -> bytes:
    """Load private key from file (PEM/DER) and return PKCS8 DER bytes."""
    try:
        with open(private_key_file_path, "rb") as key_file:
            return _private_key_bytes_to_snowflake_der(key_file.read())
    except Exception as e:
        logging.critical(f"Failed to load private key: {e}")
        raise


def load_private_key_from_vault(vault_cfg: dict) -> bytes:
    """
    Fetch private key from HashiCorp Vault using AppRole, then return PKCS8 DER bytes.

    Expected config keys:
      - addr (optional): Vault address; falls back to env VAULT_ADDR
      - role_id / role_id_env: AppRole role_id (or env var name)
      - secret_id / secret_id_env: AppRole secret_id (or env var name)
      - sign_path: Vault secret read path (e.g. /v1/Snowflake/data/Credentials)
      - secret_field (optional): which field inside the secret contains the key
    """
    addr = _cfg_str(vault_cfg.get("addr") or os.getenv("VAULT_ADDR"))
    if not addr:
        raise RuntimeError("Vault is enabled but Vault address is missing (vault.addr or VAULT_ADDR)")

    role_id = _cfg_str(vault_cfg.get("role_id"))
    secret_id = _cfg_str(vault_cfg.get("secret_id"))

    role_id_env = _cfg_str(vault_cfg.get("role_id_env")) or "ROLE_ID"
    secret_id_env = _cfg_str(vault_cfg.get("secret_id_env")) or "SECRET_ID"
    if not role_id:
        role_id = _cfg_str(os.getenv(role_id_env))
        # Backward compatibility: some configs accidentally place literal values
        # in role_id_env instead of role_id.
        if (
            not role_id
            and "role_id_env" in vault_cfg
            and role_id_env
            and not _ENV_VAR_NAME_RE.match(role_id_env)
        ):
            logger.warning(
                "Vault config key 'role_id_env' appears to contain a literal role_id. "
                "Prefer using 'vault.role_id'."
            )
            role_id = role_id_env
    if not secret_id:
        secret_id = _cfg_str(os.getenv(secret_id_env))
        # Backward compatibility: some configs accidentally place literal values
        # in secret_id_env instead of secret_id.
        if (
            not secret_id
            and "secret_id_env" in vault_cfg
            and secret_id_env
            and not _ENV_VAR_NAME_RE.match(secret_id_env)
        ):
            logger.warning(
                "Vault config key 'secret_id_env' appears to contain a literal secret_id. "
                "Prefer using 'vault.secret_id'."
            )
            secret_id = secret_id_env

    if not role_id or not secret_id:
        raise RuntimeError(
            "Vault is enabled but role_id/secret_id are missing. "
            f"Set vault.role_id and vault.secret_id in config, or set env vars {role_id_env} and {secret_id_env}."
        )

    sign_path = _cfg_str(vault_cfg.get("sign_path") or os.getenv("SIGN_PATH"))
    if not sign_path:
        raise RuntimeError("Vault is enabled but sign_path is missing (vault.sign_path or SIGN_PATH)")

    secret_field = _cfg_str(vault_cfg.get("secret_field"))

    logger.info("Vault: preparing to fetch Snowflake private key")
    token = _vault_login_approle(addr, role_id, secret_id)
    secret_data = _vault_read_kv(addr, token, sign_path)
    if not secret_data:
        raise RuntimeError(f"Vault secret at {sign_path} returned no data")

    candidates = [secret_field] if secret_field else []
    candidates += [
        "ia_dev_bi_user_rsa_key",
        "private_key",
        "privateKey",
        "private_key_pem",
        "privateKeyPem",
        "key",
    ]

    key_value = None
    for k in candidates:
        if k and k in secret_data:
            key_value = secret_data.get(k)
            break
    if key_value is None:
        raise RuntimeError(
            f"Vault secret at {sign_path} does not contain expected key field. "
            f"Available fields: {list(secret_data.keys())}"
        )

    if isinstance(key_value, str):
        s = key_value.strip()
        if s.startswith("-----BEGIN"):
            key_bytes = s.encode("utf-8")
        elif _looks_like_base64(s):
            key_bytes = base64.b64decode("".join(s.split()))
        else:
            key_bytes = s.encode("utf-8")
    elif isinstance(key_value, (bytes, bytearray)):
        key_bytes = bytes(key_value)
    else:
        raise RuntimeError(f"Unsupported Vault key field type: {type(key_value)}")

    pkb = _private_key_bytes_to_snowflake_der(key_bytes)
    logger.info("Vault: private key fetched and converted for Snowflake JWT")
    return pkb

def connect_snowflake(sf_cfg, pkb):
    """Connect to Snowflake with JWT authentication, with fallback for role issues"""
    try:
        logger.info("Snowflake: attempting connection (with role if configured)")
        return snowflake.connector.connect(
            user=sf_cfg["user"],
            account=sf_cfg["account"],
            warehouse=sf_cfg["warehouse"],
            database=sf_cfg["database"],
            schema=sf_cfg["schema"],
            role=sf_cfg.get("role"),
            authenticator=sf_cfg["authenticator"],
            private_key=pkb,
            client_session_keep_alive=True  # Keep session alive automatically
        )
    except DatabaseError:
        logger.warning("Snowflake: role not available; retrying without role")
        return snowflake.connector.connect(
            user=sf_cfg["user"],
            account=sf_cfg["account"],
            warehouse=sf_cfg["warehouse"],
            database=sf_cfg["database"],
            schema=sf_cfg["schema"],
            authenticator=sf_cfg["authenticator"],
            private_key=pkb,
            client_session_keep_alive=True  # Keep session alive automatically
        )


def _resolve_snowflake_conn_config(cfg: dict, platform: str | None = None) -> tuple[str, dict]:
    """
    Resolve Snowflake connection config by platform key, with legacy fallback.
    """
    connections = (cfg or {}).get("connections") or {}
    if not isinstance(connections, dict) or not connections:
        raise RuntimeError("No Snowflake connections found under config.connections")

    requested = (platform or "").strip().lower()
    candidate_keys = set()
    if requested:
        candidate_keys.add(requested)
        if requested.startswith("ia_"):
            candidate_keys.add(requested[3:])
        else:
            candidate_keys.add(f"ia_{requested}")

    if candidate_keys:
        for key, value in connections.items():
            if key.lower() in candidate_keys:
                if not isinstance(value, dict):
                    raise RuntimeError(f"connections.{key} must be a mapping")
                return key, value

    legacy_key = "snowflake_connection"
    if legacy_key in connections:
        legacy_value = connections[legacy_key]
        if not isinstance(legacy_value, dict):
            raise RuntimeError(f"connections.{legacy_key} must be a mapping")
        return legacy_key, legacy_value

    available = ", ".join(sorted(connections.keys()))
    raise RuntimeError(
        f"No Snowflake connection found for platform '{platform}'. "
        f"Available connection keys: {available}"
    )

async def create_pool(platform: str | None = None):
    """
    Establish a Snowflake connection with JWT authentication and stash it in `global_pool`.
    """
    global global_pool, _active_pool_conn_key
    try:
        _load_env_from_backend_dir_once()

        with open('config/config.yaml', 'r') as f:
            cfg = yaml.safe_load(f)

        requested_platform = platform or os.getenv("SNOWFLAKE_PLATFORM")
        conn_key, conn = _resolve_snowflake_conn_config(cfg, requested_platform)
        
        # Load private key for JWT authentication (Vault or file fallback)
        private_key_source = (conn.get("private_key_source") or "").strip().lower()
        if private_key_source == "vault" or isinstance(conn.get("vault"), dict):
            vault_cfg = dict(conn.get("vault") or {})
            # Backward compatibility: allow Vault fields directly under the
            # connection block (outside `vault:`).
            for key in (
                "addr",
                "role_id",
                "secret_id",
                "role_id_env",
                "secret_id_env",
                "sign_path",
                "secret_field",
            ):
                if key not in vault_cfg and key in conn:
                    vault_cfg[key] = conn.get(key)
            private_key = load_private_key_from_vault(vault_cfg)
        else:
            raise RuntimeError(
                "Snowflake private key is configured as Vault-only. "
                f"Set connections.{conn_key}.private_key_source: vault and provide a vault: block."
            )
        
        # Connect to Snowflake using the new connection function
        global_pool = connect_snowflake(conn, private_key)
        _active_pool_conn_key = conn_key
        logger.info("Snowflake: connected successfully")
        return global_pool

    except Exception as e:
        logging.critical(f"Failed to create Snowflake connection: {e}")
        raise

async def check_and_reconnect_db():
    """
    If `global_pool` is missing or closed, re-run create_pool().
    With client_session_keep_alive=True, token expiration is handled automatically.
    """
    global global_pool, _active_pool_conn_key
    
    if global_pool is None:
        print("Connection is None. Need to reconnect.")
    elif getattr(global_pool, 'is_closed', lambda: True)():
        print("Connection is closed. Need to reconnect.")
    else:
        # Connection exists and appears open, return early
        return
    
    # Only reconnect if connection is missing or closed
    try:
        print("Reconnecting to Snowflake...")
        # Close old connection if it exists
        if global_pool is not None:
            try:
                global_pool.close()
            except:
                pass
        await create_pool(_active_pool_conn_key)
        print("✅ Reconnected to Snowflake successfully.")
    except Exception as e:
        logging.critical(f"Failed to reconnect to Snowflake: {e}")
        raise

@retry(stop=stop_after_attempt(2), wait=wait_fixed(0.5), reraise=True)
async def executeQuery(query):
    '''Async function to execute a database query with automatic reconnection on token expiration'''
    global global_pool
    
    try:
        await check_and_reconnect_db()
        if global_pool is None:
            raise ConnectionError("Snowflake connection is not initialized.")

        # run the blocking cursor call in a thread
        loop = asyncio.get_event_loop()
        def _run():
            cs = global_pool.cursor()
            try:
                cs.execute(query)
                rows = cs.fetchall()
                desc = cs.description
            finally:
                cs.close()
            return rows, desc

        rows, desc = await loop.run_in_executor(None, _run)

        if rows:
            colnames = [col[0] for col in desc]
            data_rows = [list(r) for r in rows]
            result = [colnames] + data_rows
        else:
            logging.warning(f"Query ran but returned no data: {query}")
            result = []

        return result, 0, 0

    except snowflake.connector.errors.DatabaseError as e:
        error_msg = str(e)
        # Check if it's a token expiration error
        if "390114" in error_msg or "Authentication token has expired" in error_msg:
            logging.warning(f"Token expired, attempting to reconnect: {e}")
            # Force reconnection
            if global_pool is not None:
                try:
                    global_pool.close()
                except:
                    pass
                global_pool = None
            # Retry will be handled by @retry decorator
            raise
        else:
            error_details = f"DB SQL Error: {e} | Query: {query}\n{traceback.format_exc()}"
            logging.error(error_details)
            return "", 1, e
    except Exception as e:
        error_details = f"Unexpected Error: {e} | Query: {query}\n{traceback.format_exc()}"
        logging.error(error_details)
        return "", 1, e

async def execute_write_query(query):
    '''Async function to execute a write query with automatic reconnection on token expiration'''
    global global_pool
    
    try:
        await check_and_reconnect_db()
        loop = asyncio.get_event_loop()
        def _run():
            cs = global_pool.cursor()
            try:
                cs.execute(query)
            finally:
                cs.close()
            return "Write operation successful"
        result = await loop.run_in_executor(None, _run)
        logging.info(f"Write operation successful: {query}")
        return result, 0, 0

    except snowflake.connector.errors.DatabaseError as e:
        error_msg = str(e)
        # Check if it's a token expiration error
        if "390114" in error_msg or "Authentication token has expired" in error_msg:
            logging.warning(f"Token expired during write operation, reconnecting: {e}")
            # Force reconnection
            if global_pool is not None:
                try:
                    global_pool.close()
                except:
                    pass
                global_pool = None
            # Raise to trigger retry
            raise
        else:
            error_details = f"DB SQL Error: {e} | Query: {query}\n{traceback.format_exc()}"
            logging.error(error_details)
            return "", 1, e
    except Exception as e:
        error_details = f"Unexpected Error: {e} | Query: {query}\n{traceback.format_exc()}"
        logging.error(error_details)
        return "", 1, e
