# Nosana scorer — build, deploy, and wire up

The gateway works today without any of this: with `NOSANA_SCORER_URL` empty it
scores with the deterministic local fallback (`score_source=fallback`). Follow
these steps to flip it to live GPU scoring (`score_source=nosana`). Steps
marked **(manual)** need your accounts/credits and cannot be automated.

## 1. Local smoke test (no Docker needed)

```powershell
cd nosana-service
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SCORER_TOKEN = "local-test-token"
uvicorn server:app --port 8001
```

Then:

```
curl http://localhost:8001/health
curl -X POST http://localhost:8001/score -H "Content-Type: application/json" -H "X-Scorer-Token: local-test-token" -d "{\"query\":\"fix authentication retry bug\",\"items\":[{\"unit_id\":\"u1\",\"text\":\"RetryManager throws timeout after three attempts\",\"content_type\":\"log\"},{\"unit_id\":\"u2\",\"text\":\"CSS build completed successfully\",\"content_type\":\"log\"}]}"
```

`u1` must score clearly higher than `u2`.

Tip: to test the full gateway against this local scorer before touching
Nosana, set in `.env`: `NOSANA_SCORER_URL=http://localhost:8001` and
`NOSANA_SCORER_TOKEN=local-test-token`.

## 2. Build and push the image **(manual: Docker Hub login)**

```powershell
# generate the application token that will protect /score
python -c "import secrets; print(secrets.token_urlsafe(32))"

$env:IMAGE="docker.io/perynnneo/chunkit-nosana-scorer:hackathon"
echo $env:DOCKER_TOKEN | docker login -u perynnneo --password-stdin
docker build -t $env:IMAGE nosana-service
docker push $env:IMAGE
```

Keep the image public — a private registry adds pull credentials to the job
definition and one more way for the deployment to fail.

## 3. Create the Nosana deployment **(manual: Nosana account + credits)**

1. Log in to Nosana Deploy; create a named API key (expiry after the event) → `NOSANA_API_KEY`.
2. Confirm the account balance / sponsor credits.
3. Pick an available GPU market; copy its address into `deployment.template.json` (`market`) and `.env` (`NOSANA_MARKET`).
4. In `deployment.template.json`, replace `REPLACE_WITH_RANDOM_APPLICATION_TOKEN` with the token from step 2 (same value goes in `.env` as `NOSANA_SCORER_TOKEN`). Do NOT commit the filled file.
5. Create the deployment with the template, start it, wait for RUNNING.
   Strategy is `SIMPLE-EXTEND`: it keeps extending while funds remain — **stop/archive it after the demo.**
6. Copy the service URL for exposed port 8000 → `.env` `NOSANA_SCORER_URL`.
7. Record the deployment id → `.env` `NOSANA_DEPLOYMENT_ID`.

## 4. Verify from your machine

```
curl <NOSANA_SCORER_URL>/health
```

Wait for `"status":"ready"` and `"device":"cuda"` (first start downloads the
model). Then run the same `/score` curl as step 1 against the live URL.

## 5. Wire the gateway and prove it live

Fill in `.env`: `NOSANA_SCORER_URL`, `NOSANA_SCORER_TOKEN` (leave
`NOSANA_ENABLED=true`). Restart the gateway. A request through
`/v1/chat/completions` should now report `"score_source": "nosana"` with a
real `nosana_ms` in `debug`.

Optional live test suite:

```powershell
$env:RUN_LIVE_NOSANA_TESTS = "1"
python -m pytest tests/live -q
```

Prove the fallback for the demo: set an invalid `NOSANA_SCORER_URL`, restart,
send the same request — it must complete with `"score_source": "fallback"`.

## After the event **(manual)**

- Stop/archive the deployment (SIMPLE-EXTEND spends while funds remain).
- Rotate/revoke `NOSANA_SCORER_TOKEN`, the Nosana API key, and the Docker token.
