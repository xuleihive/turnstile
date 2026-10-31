"""Connection settings shared by the long-lived PostgreSQL pools of the API process."""

# Bound new connections and let TCP drop connections that a stopped or moved server no
# longer answers, so a pool recovers after a database restart without restarting the API.
CONNECTION_HEALTH = {
    "connect_timeout": 10,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 30_000,
}
