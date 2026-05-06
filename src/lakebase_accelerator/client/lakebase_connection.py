"""Lakebase connection management with OAuth token rotation.

Lakebase uses OAuth tokens (60-minute lifetime) for authentication.
This module supports two auth modes:
1. OAuth via Databricks REST API (generate_database_credential)
2. Static password (for local dev or when OAuth isn't configured)

For Databricks Apps deployment, credentials are auto-injected.
"""

import httpx
from psycopg2 import pool

from lakebase_accelerator.utils.logger import logger


class LakebaseConnectionPool:
    """Connection pool for Lakebase with OAuth token support.

    Generates fresh OAuth tokens via the Databricks REST API when
    LAKEBASE_ENDPOINT_NAME is configured. Falls back to static password otherwise.
    """

    def __init__(
        self,
        host: str,
        database: str = "databricks_postgres",
        user: str = "",
        port: int = 5432,
        min_connections: int = 2,
        max_connections: int = 10,
        # OAuth config
        databricks_host: str = "",
        client_id: str = "",
        client_secret: str = "",
        endpoint_name: str = "",
        # Static password fallback
        static_password: str = "",
    ) -> None:
        self._host = host
        self._database = database
        self._user = user
        self._port = port
        self._min_connections = min_connections
        self._max_connections = max_connections
        self._databricks_host = databricks_host.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._endpoint_name = endpoint_name
        self._static_password = static_password
        self._use_oauth = bool(endpoint_name and client_id and client_secret)
        self._pool: pool.ThreadedConnectionPool | None = None

    def initialize(self) -> None:
        """Initialize the connection pool with a fresh token or static password."""
        password = self._get_password()

        self._pool = pool.ThreadedConnectionPool(
            minconn=self._min_connections,
            maxconn=self._max_connections,
            host=self._host,
            port=self._port,
            database=self._database,
            user=self._user,
            password=password,
            sslmode="require",
        )
        logger.info(
            f"Lakebase connection pool initialized "
            f"(host: {self._host}, user: {self._user}, oauth: {self._use_oauth})"
        )

    def getconn(self):
        """Get a connection from the pool."""
        if self._pool is None:
            raise RuntimeError("Connection pool not initialized")

        conn = self._pool.getconn()

        # Test if connection is still alive (token may have expired)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.commit()
        except Exception:
            # Connection is dead — reconnect with fresh token
            logger.warning("Stale connection detected, refreshing pool with new token")
            try:
                self._pool.putconn(conn, close=True)
            except Exception:
                pass
            self._refresh_pool()
            conn = self._pool.getconn()

        return conn

    def putconn(self, conn, close: bool = False) -> None:
        """Return a connection to the pool."""
        if self._pool:
            try:
                self._pool.putconn(conn, close=close)
            except Exception:
                pass

    def closeall(self) -> None:
        """Close all connections in the pool."""
        if self._pool:
            self._pool.closeall()
            logger.info("Lakebase connection pool closed")

    def _get_password(self) -> str:
        """Get the database password — either via OAuth token or static."""
        if not self._use_oauth:
            if self._static_password:
                return self._static_password
            raise RuntimeError(
                "No authentication configured. Set either LAKEBASE_ENDPOINT_NAME "
                "(for OAuth) or POSTGRES_PASSWORD (for static auth)."
            )

        return self._generate_oauth_token()

    def _generate_oauth_token(self) -> str:
        """Generate a Lakebase OAuth token via Databricks REST API.

        Steps:
        1. Exchange SP client_id + client_secret for a workspace OAuth token
        2. Exchange the workspace token for a database credential
        """
        # Step 1: Get workspace OAuth token
        workspace_token = self._get_workspace_token()

        # Step 2: Exchange for database credential
        db_token = self._get_database_credential(workspace_token)

        logger.info("Generated fresh Lakebase OAuth token")
        return db_token

    def _get_workspace_token(self) -> str:
        """Exchange SP credentials for a workspace OAuth token."""
        token_url = f"{self._databricks_host}/oidc/v1/token"

        response = httpx.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "scope": "all-apis",
            },
            auth=(self._client_id, self._client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30.0,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to get workspace token: {response.status_code} — {response.text}"
            )

        return response.json()["access_token"]

    def _get_database_credential(self, workspace_token: str) -> str:
        """Exchange workspace token for a Lakebase database credential."""
        url = f"{self._databricks_host}/api/2.0/postgres/credentials"

        response = httpx.post(
            url,
            json={"endpoint": self._endpoint_name},
            headers={
                "Authorization": f"Bearer {workspace_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to get database credential: {response.status_code} — {response.text}"
            )

        return response.json()["token"]

    def _refresh_pool(self) -> None:
        """Refresh the pool with a new OAuth token."""
        if self._pool:
            try:
                self._pool.closeall()
            except Exception:
                pass
        self.initialize()


def create_lakebase_pool(workspace_client, settings) -> "LakebaseConnectionPool | None":
    """Factory function to create a LakebaseConnectionPool from settings."""
    if not settings.postgres_host:
        logger.warning("POSTGRES_HOST not set — skipping Lakebase connection")
        return None

    lakebase_pool = LakebaseConnectionPool(
        host=settings.postgres_host,
        database=settings.postgres_db,
        user=settings.postgres_user,
        port=settings.postgres_port,
        min_connections=settings.db_pool_min_connections,
        max_connections=settings.db_pool_max_connections,
        # OAuth config
        databricks_host=settings.databricks_host,
        client_id=settings.databricks_client_id,
        client_secret=settings.databricks_client_secret,
        endpoint_name=settings.lakebase_endpoint_name,
        # Static fallback
        static_password=settings.postgres_password,
    )

    try:
        lakebase_pool.initialize()
        return lakebase_pool
    except Exception as e:
        logger.warning(f"Failed to initialize Lakebase connection pool: {e}. Running in degraded mode.")
        return None
