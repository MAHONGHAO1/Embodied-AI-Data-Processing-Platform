# QuicTrain ECS single-node deployment

Shared-edge mode (default on the co-located ECS with `/opt/caddy`):

- This Compose stack does **not** publish 80/443 and has **no** in-stack `gateway`.
- Host Caddy terminates TLS only for the training hostname (no edge Basic Auth — app `/login`
  owns multi-user auth with `QUICTRAIN_AUTH_MODE=local`).
- Loopback publishes for the shared reverse proxy only:
  - API `127.0.0.1:8001`
  - Web `127.0.0.1:3000`
  - MLflow `127.0.0.1:5001`
- PostgreSQL stays on the Compose network (never host `:5432` — reserved for the data platform).
- Data platform continues to own host `:8000` / `:5432` / `:6379`.

Required host-local files (never commit them):

- `.env`, based on `.env.example` (`QUICTRAIN_AUTH_MODE=local`).
- `secrets/postgres_password`
- `secrets/admin_password` (plaintext admin password for seeded `QUICTRAIN_ADMIN_EMAIL`)
- `secrets/bootstrap_admin_token` (may be empty)
- Optional `secrets/web_password_hash` only if you keep break-glass Basic Auth on `mlflow.*`

Start or upgrade with:

```bash
mkdir -p infra/ecs/secrets && chmod 700 infra/ecs/secrets
touch infra/ecs/secrets/bootstrap_admin_token
# printf '%s' 'your-strong-password' > infra/ecs/secrets/admin_password && chmod 600 infra/ecs/secrets/admin_password

./scripts/ecs_preflight.sh
docker compose --env-file infra/ecs/.env -f infra/ecs/docker-compose.yml up -d --build --remove-orphans
```

Verify loopback `curl -fsS http://127.0.0.1:8001/health` (identity.auth_mode=local), then open
`https://$PUBLIC_HOSTNAME/login`. See `docs/runbooks/ecs-single-node.md` and
`docs/runbooks/identity-bootstrap.md`.
