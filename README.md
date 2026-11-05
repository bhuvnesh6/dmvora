# DMvora

Small, single-user WhatsApp CRM built with Flask + SQLite. Default app port: **8822**.

## Included
- Single admin login from `.env`.
- Multiple Meta WhatsApp number connections using Embedded Signup.
- Separate inboxes and lead lists per selected `phone_number_id`.
- Incoming message payloads saved in SQLite, including non-text message types and raw webhook JSON.
- Text replies from the inbox.
- Per-number keyword and first-inbound-after-24-hours automations.
- Drag/drop node palette with message, image, and form nodes; up to three interactive reply buttons.
- Native WhatsApp Flows forms; submitted answers update the matching lead.
- Meta template sync and basic template submission.

## Important WhatsApp behavior
- The free-form customer-service window is 24 hours from the customer's latest message. Outside it, use an approved template.
- WhatsApp interactive buttons in a normal session message are **reply buttons**, not arbitrary URL buttons. DMvora's website action uses a reply button and then sends the URL as a follow-up text. A true URL CTA button must be part of an approved Meta template.
- Form nodes and form buttons send a native, published WhatsApp Flow. Customers complete the form inside WhatsApp; DMvora saves the returned `nfm_reply` answers to the matching lead.
- Image URLs must be publicly reachable over HTTPS.
- Incoming media metadata and media IDs are saved; this MVP does not download media binaries from Meta.

## Run locally
```bash
cp .env.example .env
# Edit .env and set strong secrets/password
pip install -r requirements.txt
python app.py
```
Open http://localhost:8822/login

## Hostinger Docker VPS
```bash
git clone YOUR_REPOSITORY_URL dmvora
cd dmvora
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f dmvora
```
After future code updates:
```bash
cd dmvora
git pull
docker compose up -d --build
docker compose logs --tail=100 -f dmvora
```
SQLite persists in `./data/dmvora.sqlite3`; do not delete `data/` when redeploying.

## Reverse proxy
Point `dmvora.sanjivanitechno.com` to the VPS service on port `8822`. Enable HTTPS. Do not expose port 8822 publicly if your reverse proxy can route internally; if publishing it, firewall it to trusted networks.

## Meta configuration
- Keep the Meta app webhook callback set to `https://pravahai.sanjivanitechno.com/webhook/meta-app`.
- Keep Meta's data deletion callback at `https://pravahai.sanjivanitechno.com/meta/data-deletion`.
- Add `dmvora.sanjivanitechno.com` to **Allowed Domains for the JavaScript SDK**. Enter hostnames, not a path.
- Embedded Signup app ID, app secret and config ID in DMvora must match the same Meta app/config used by PravahAI.
- The verify token is used by the Meta app-level webhook at PravahAI, not by DMvora's internal event endpoint.

## PravahAI forwarding patch
Add these imports near the top of PravahAI's `app.py` if not already present:
```python
import os, time, hmac, hashlib, requests, threading
```
Add the following helper once:
```python
DMVORA_WEBHOOK_URL = os.getenv(
    "DMVORA_WEBHOOK_URL",
    "https://dmvora.sanjivanitechno.com/internal/meta/events"
).strip()
DMVORA_WEBHOOK_SECRET = os.getenv("DMVORA_WEBHOOK_SECRET", "").strip()

def forward_meta_event_to_dmvora(payload):
    if not DMVORA_WEBHOOK_URL or not DMVORA_WEBHOOK_SECRET:
        return
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        DMVORA_WEBHOOK_SECRET.encode(),
        timestamp.encode() + b"." + raw,
        hashlib.sha256
    ).hexdigest()
    try:
        requests.post(
            DMVORA_WEBHOOK_URL,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Pravah-Timestamp": timestamp,
                "X-Pravah-Signature": signature,
            },
            timeout=5,
        )
    except requests.RequestException as exc:
        log("DMVORA-FORWARD", f"forward failed: {exc}")
```
Then inside existing `meta_app_webhook()` in PravahAI, after the raw Meta signature has been verified and `payload` has been JSON-decoded, add:
```python
# Forward the original event once. DMvora performs its own phone_number_id filter.
threading.Thread(
    target=forward_meta_event_to_dmvora,
    args=(payload,),
    daemon=True,
).start()
```
Do not change the existing owner-routing loop or `_handle_meta_payload` call. Forward before the final `EVENT_RECEIVED` response. The forwarding call is independent of PravahAI's own event processing.

Add to **PravahAI's** `.env`:
```env
DMVORA_WEBHOOK_URL=https://dmvora.sanjivanitechno.com/internal/meta/events
DMVORA_WEBHOOK_SECRET=THE_EXACT_SAME_RANDOM_SECRET_AS_DMVORA
```
Add the identical value to DMvora's `PRAVAH_WEBHOOK_SECRET`.

## Event isolation
DMvora extracts `entry[].changes[].value.metadata.phone_number_id`. It queries only its own `numbers` table. If there is no exact active match, it ignores the event and returns HTTP 200. It does not search by display phone, contact phone, WABA alone, or any PravahAI customer ID. The `phone_number_id` is the routing key.

## Operational cautions
- This is a single-user MVP, not a multi-tenant SaaS. Protect `.env`, SQLite backups and the admin password.
- A daemon thread is adequate for an initial low-volume single-user deployment but is not a durable queue. For production reliability, replace it with a persistent job queue and retry failed Meta sends/forwards.
- Use a persistent Docker volume for `/data`.
- Set up regular backups of `./data/dmvora.sqlite3`.


## Native WhatsApp in-chat forms (WhatsApp Flows)

The Forms area now connects **published Meta WhatsApp Flows**. It does not generate a Meta Flow JSON designer or create/publish Flows automatically.

1. In WhatsApp Manager, open **Flows** for the same WABA as the connected DMvora number.
2. Create the form screens and input fields, validate, and publish the Flow.
3. Copy the Flow ID and the first screen ID (for example `WELCOME`).
4. In DMvora → Forms, save the Flow ID, screen ID, CTA label and launch message.
5. In Automations, add a Form node or choose **Open WhatsApp Flow** on a quick-reply button.
6. When the customer submits the form in WhatsApp, Meta sends an `interactive.nfm_reply` webhook. DMvora correlates its `flow_token` to the exact number and lead and stores the submitted JSON in that lead's `form_data`.

The customer completes the form inside WhatsApp. No external browser form link is sent by the automation.

Meta Flow requirements:
- The Flow must be published and usable by the same WABA/phone-number business.
- `flow_cta` is limited to 20 characters.
- The configured first screen ID must exactly match the first screen ID in the published Flow.
- The connected number's access token must have the required WhatsApp permissions.
- Meta must deliver `nfm_reply` events through the existing PravahAI → DMvora webhook forwarding path.


## Native WhatsApp in-chat forms (WhatsApp Flows)

The Forms area connects **published Meta WhatsApp Flows**. It does not currently generate a Flow JSON designer or create/publish Flows automatically.

1. In WhatsApp Manager, open **Flows** for the same WABA as the connected DMvora number.
2. Create the form screens and input fields, validate, and publish the Flow.
3. Copy the Flow ID and first screen ID (for example `WELCOME`).
4. In DMvora → Forms, save the Flow ID, screen ID, CTA label and launch message.
5. In Automations, add a Form node or choose **Open WhatsApp Flow** on a quick-reply button.
6. On submission, Meta sends an `interactive.nfm_reply` webhook. DMvora correlates the `flow_token` to the exact number and lead and saves the submitted JSON in that lead's `form_data`.

The customer completes the form inside WhatsApp. No external browser form link is sent by the automation. The Flow must be published and available to the same WABA; the configured first screen ID must match exactly.
