# Connecting the backend to Vapi

**Status: not yet done.** No Vapi account was used to build this repo, so none of these steps has been run. The JSON field names and the credential behaviour come from Vapi's docs, read on 2026-11-17:
- [custom tools](https://docs.vapi.ai/tools/custom-tools)
- [server authentication](https://docs.vapi.ai/server-url/server-authentication)

Dashboard menu labels may differ from the wording below.

## The files here

| File | What it is |
|---|---|
| `tools.json` | The 5 function tools, each in the body shape Vapi documents for `POST https://api.vapi.ai/tool`: `type`, `function` (`name`, `description`, `parameters`), `server` (`url`, `credentialId`) |
| `assistant.json` | The assistant: `name`, `firstMessage`, and `model` (system prompt in `messages`, tools attached by `toolIds`) |

Both files are generated from `app/vapi.py` by `make vapi-config`. `tests/test_vapi_config.py` fails if either one drifts from the code, so edit `app/vapi.py`, never the JSON. `SERVER_URL`, `CREDENTIAL_ID` and the `ID-OF-…` tool ids are placeholders.

**No API registration script.** Vapi's docs confirm the tool-creation body, but the credential-creation endpoint page returned 404. Parts of the assistant schema weren't readable either. A script that has never run against the real API would be a guess, so this guide covers the dashboard only.

## Steps

1. **Run the backend with a real secret.**

   ```bash
   make up
   python -c "import secrets; print(secrets.token_urlsafe(32))"   # put this in .env as VAPI_SECRET
   make run                                                        # http://localhost:8004
   ```

2. **Give it a public HTTPS URL.** Vapi has to reach your machine, so start one of these tunnels:

   ```bash
   cloudflared tunnel --url http://localhost:8004    # prints https://<random>.trycloudflare.com
   # or
   ngrok http 8004                                   # prints an https "Forwarding" URL
   ```

   Then check the tunnel from outside:
   - `curl https://<host>/health` should return `{"status":"ok"}`.
   - `curl -i -X POST https://<host>/vapi/tool-calls` should return `401`, because a request without the secret is refused.

3. **Create the credential.** Create a **Bearer Token** credential:
   - **Token:** your `VAPI_SECRET`.
   - **Header Name:** keep the default `Authorization`.
   - **Include Bearer Prefix:** on.

   Vapi then sends `Authorization: Bearer <VAPI_SECRET>`, which is exactly what `app/main.py` checks. Note the credential's id.

4. **Create the five tools.** For each entry in `tools.json`, create a function tool:
   - copy its `name`, `description` and `parameters`;
   - set the server URL to `https://<host>/vapi/tool-calls`;
   - attach the credential from step 3.

   Note each tool's id. Keep the tools synchronous (the default `async: false`), so the agent waits for the backend's answer.

5. **Create the assistant.**
   - Set the system prompt to `model.messages[0].content` from `assistant.json`, and the first message to `firstMessage`.
   - Attach the five tools.
   - Pick any tool-calling model: `assistant.json` uses `openai` / `gpt-4o` only because that is the example in Vapi's docs.
   - Voice and transcriber: the dashboard defaults are fine.

6. **Attach a phone number and call it** (phone numbers are paid). Say something like "Can I book Friday at 2pm?". Then confirm the booking and its latency were recorded:

   ```bash
   docker compose exec db psql -U app -d app -c "SELECT * FROM bookings"
   docker compose exec db psql -U app -d app -c \
     "SELECT tool, ok, round(latency_ms::numeric, 1) FROM tool_calls ORDER BY id DESC LIMIT 5"
   ```

## Things to check on the first real call

- **The caller's phone number.**
  - Bookings belong to the number, which `app/vapi.py` reads from `message.call.customer.number`, falling back to `message.customer.number`. Vapi's docs don't show either path.
  - If booking replies "no caller phone number", the server log prints the keys the `call` object actually had. Fix `caller_number()` to match.
  - Dashboard web test calls have no phone number at all. They can check availability, but booking is refused by design.
- **Timeouts.** Vapi's default timeout for function tools isn't stated in the docs read. The main README has this backend's latency, measured locally; the tunnel adds network time on top.
- **Authentication.** Vapi's docs warn that if you supply a server URL in an API request, your endpoint must authenticate the request itself. This one does: anything without the secret gets 401.
