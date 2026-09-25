# Public demo

One VM runs a dst deployment anyone can sign in to and ask. The warehouse is MotherDuck,
sign-in is Clerk, the model is whatever `DST_PROVIDERS` names. Every visitor lands in one
seeded org as a non-admin caller in group `demo`, keyed by their sign-in email, and draws
against three budgets: a per-minute limit, a per-day quota on the lens, and a daily cap for
the whole deployment. A refusal says which one and when it frees. Admin never leaves the
VM: sign-in cannot reach the control plane in demo mode, only a `dstadm_` token can.

What is in this directory:

| File | What |
|---|---|
| `docker-compose.yml` | Postgres, the app (no published port), Caddy with TLS |
| `Caddyfile` | one site block, proxied to the app |
| `.env.example` | the full env contract for the demo |
| `load_motherduck.py` | copies a `.duckdb` file into MotherDuck as one database |

## Before the VM

1. **MotherDuck.** Create an account, mint a read-write token, and load the data:

   ```
   MOTHERDUCK_TOKEN=<read-write> python deploy/demo/load_motherduck.py \
       --source fixtures/jaffle_shop.duckdb --database dst_demo
   ```

   Then mint a **read-scaling token** for the same account. That is the token the demo
   serves with: it can only `SELECT`, so the credential itself cannot write, beneath the
   connector's read-only open. Read-scaling connections need the database to exist already, which
   the load above did.
2. **Clerk.** Create an application; note the publishable and secret keys. Sign-in method
   is your call; every method ends in an email or a subject id, and that is the caller name.
3. **DeepSeek.** Use a key of its own on a prepaid balance you top up by hand, so the
   spend cannot run past what you loaded. dst's daily cap bounds answers, not dollars,
   and a cap that lives only in the code is a single point of failure.
4. **DNS.** Point `DEMO_DOMAIN` at the VM. Caddy gets the certificate on first request.

## On the VM

```
git clone https://github.com/get-dst/dst && cd dst/deploy/demo
cp .env.example .env            # fill everything except DST_DEMO_ORG_ID
docker compose up -d
docker compose exec app dst bootstrap --org demo
```

`bootstrap` prints the org id and an admin token. Put the org id in `.env` as
`DST_DEMO_ORG_ID`, keep the token somewhere private, and restart the app:

```
docker compose up -d app
docker compose exec app dst demo --org-id <org id> \
    --path md:dst_demo --secret-env MOTHERDUCK_TOKEN \
    --statement-timeout-ms 30000 --allow-group demo --per-caller-rpd 50
```

`dst demo` probes the path with the token before it writes anything, publishes the lens
granted to group `demo`, and bounds each caller to 50 answers a day. Re-run it to change
any of that. Open `https://<DEMO_DOMAIN>/demo`: sign in, get a key, ask.

The three doors a visitor gets, all governed by the same lens allow-list and budgets:

- `POST /v1/lenses/customer_value/query` with the key as a bearer
- `/v1` as an OpenAI-compatible base URL, the lens as the model name
- `/mcp` from any MCP client; the OAuth consent page signs them in with the same account

## The same thing on Cloud Run

The VM is the recipe; the same service also runs on Cloud Run, following the Cloud Run
section of [docs/deployment.md](../../docs/deployment.md#cloud-run-and-friends). The
demo-specific parts map onto it like this:

- The service gets the extra env: `DST_DAILY_REQUEST_CAP`, `DST_CLERK_PUBLISHABLE_KEY`,
  `DST_LLM_DESCRIPTIONS=false`, and `DST_DEMO_ORG_ID` once bootstrap has printed the org.
- `dst demo` and `dst prune-log` run as Cloud Run Jobs shaped like the migrate job. Pass
  repeated values with `=` (`--allow-group=demo`), since `gcloud --args` rejects a value
  that appears twice in the list.
- Without a MotherDuck token the lens reads the fixture inside the image, which is enough
  to prove the doors; `--path md:<database> --secret-env MOTHERDUCK_TOKEN` moves it.

## Keeping it bounded

- **Questions are logged** with the caller's email. The page says so. Give them the
  bounded life it promises with a cron line on the VM:

  ```
  17 3 * * * cd /path/to/dst/deploy/demo && docker compose exec -T app dst prune-log --keep-days 30
  ```
- **The rate limiter is per process.** This compose runs one app container, so the
  per-minute budget is exact. Scale it out and the per-minute limit multiplies; the daily
  quotas do not, they are counted in Postgres.
- **Upgrades.** Pin `DST_IMAGE` to a release tag. Migrations run on start.
- **Data swap.** Load another `.duckdb` into MotherDuck under a new name and re-run
  `dst demo --path md:<name>`. The lens is the bundled one; a different lens over
  different data is a project directory and `dst apply`, as for any deployment.
