# CRM Inbox Fully Implemented Features — LLM Replication Guide

## Purpose

Use this document to reproduce the fully implemented functionality from `/admin/crm/inbox` in another platform.

This guide covers only:

1. Facebook and Instagram comment replies.
2. AI reply drafting and voice input.
3. Dynamic WhatsApp template fields and WhatsApp contact lookup.
4. Email CC/BCC delivery.

The implementation must connect the visible controls to real permissions, validation, database records and external providers. Do not build these as preview-only controls.

## Shared rules

- Every action requires an authenticated user.
- Every form-changing action must include CSRF protection.
- The server must enforce permissions; hiding a button is not sufficient.
- Never expose access tokens or API keys to the browser.
- Record outbound messages before or while sending so success and failure are visible later.
- Never mark a message as sent before the provider confirms it.
- Show a clear, safe error to the user while retaining the detailed provider error in server logs or internal message metadata.
- Do not automatically send AI-generated text. The agent must review and submit it.

---

## 1. Facebook and Instagram comment replies

### What the user sees

When a social comment is selected, the main thread shows:

- The comment author.
- Facebook or Instagram badge.
- Comment date and time.
- Original comment text.
- A “View Post” link when Meta provides a permalink.
- Existing replies displayed below the original comment.
- A reply textarea and “Reply” button.

The textarea is required and has a browser limit of 2,200 characters. This keeps the shared interface within Instagram’s limit, even though Facebook permits a longer reply.

After sending:

- Keep the user on the inbox.
- Keep the same selected comment, target and search context.
- Show the new reply in the thread.
- Show a success state when Meta accepts the reply.
- Show “Reply failed. Please try again.” when it fails.

### Required records

Store parent comments separately from replies.

#### Parent comment

Required fields:

- Internal UUID.
- Platform: `facebook` or `instagram`.
- Provider comment ID.
- Provider post/media ID.
- Source Page or Instagram Business Account ID.
- Author ID and name.
- Comment text.
- Provider creation time.
- Permalink.
- Raw provider payload.
- Active flag.
- Created and updated timestamps.

The pair `(platform, provider comment ID)` must be unique.

#### Comment reply

Required fields:

- Internal UUID.
- Parent comment UUID.
- Platform.
- Provider reply ID.
- Reply author ID and name when available.
- Reply text.
- Provider creation time.
- Raw provider response.
- Active flag.
- Created and updated timestamps.

The pair `(platform, provider reply ID)` must be unique when the provider ID is present.

### Permission rule

Use the same permission as writing to the inbox. In this app, comment replies are allowed when the user can write to the CRM inbox.

Reject the server request when the user lacks permission, even if the button was visible because of stale page data.

### Browser request

Submit:

```text
POST /admin/crm/inbox/comments/{internal_comment_id}/reply?next=/admin/crm/inbox
Content-Type: application/x-www-form-urlencoded

message=Thank you for contacting us.
_csrf_token=...
```

Preserve these inbox values in the redirect when present:

- `comment_id`
- `target_id`
- `search`

Use a `303` redirect after the POST.

### Server flow

1. Authenticate the user.
2. Check inbox-write permission.
3. Load the parent comment using its internal UUID.
4. Trim the reply.
5. Reject an empty reply.
6. Enforce the provider limit:
   - Facebook: 8,000 characters.
   - Instagram: 2,200 characters.
7. Confirm the parent has:
   - Provider comment ID.
   - Source Page or Instagram account ID.
8. Resolve the correct provider token on the server.
9. Send the reply to Meta.
10. Only after Meta accepts it, save the reply with the returned provider ID and raw response.
11. Write an audit event such as `reply_comment`, including comment ID and acting user.
12. Invalidate the cached comments list/thread.
13. Redirect back with `reply_sent=1`.

If the provider rejects the request, do not create a false successful reply. Redirect with:

```text
reply_error=1
reply_error_detail=Reply failed. Please try again.
```

### Meta provider calls

#### Facebook

Required token scope:

```text
pages_manage_posts
```

Request:

```text
POST {META_GRAPH_BASE_URL}/{facebook_comment_id}/comments
Content-Type: application/x-www-form-urlencoded

message={reply_text}
access_token={server_side_page_token}
```

Expected response:

```json
{
  "id": "provider_reply_id"
}
```

#### Instagram

Required token scope:

```text
instagram_manage_comments
```

Request:

```text
POST {META_GRAPH_BASE_URL}/{instagram_comment_id}/replies
Content-Type: application/x-www-form-urlencoded

message={reply_text}
access_token={server_side_instagram_or_page_token}
```

Expected response:

```json
{
  "id": "provider_reply_id"
}
```

Retry temporary Meta request failures with a small bounded retry policy. Do not retry permanent permission, validation or authentication errors indefinitely.

### Reply synchronization

Provider replies received during comment synchronization or webhook processing must use an upsert:

- Match by platform and provider reply ID.
- Update changed text, author, timestamps and raw payload.
- Do not overwrite a stored non-empty message or author with an empty provider value.
- Reattach the reply to the correct parent if necessary.
- Exclude inactive replies from the displayed thread.
- Sort displayed replies by provider creation time, then local creation time.

### Acceptance checks

- Facebook reply reaches Meta and is stored with the returned ID.
- Instagram reply reaches Meta and is stored with the returned ID.
- Empty replies are rejected.
- A reply over the platform limit is rejected before calling Meta.
- Missing provider identifiers are rejected.
- Missing/invalid token produces a visible failure, not a fake reply.
- Users without inbox-write permission cannot reply.
- Retrying or re-syncing the same provider reply does not create a duplicate.
- Existing replies remain visible after refreshing the page.

### Source references

- `templates/admin/crm/_comment_thread.html`
- `app/web/admin/crm_inbox_comment_reply.py`
- `app/services/crm/inbox/comment_replies.py`
- `app/services/crm/conversations/comments.py`
- `app/services/meta_pages.py`
- `app/models/crm/comments.py`
- `tests/test_social_comment_replies.py`

---

## 2. AI reply drafting and voice input

This is one feature group with three connected actions:

1. Generate an AI reply draft from the selected conversation.
2. Record speech and convert it to text.
3. Polish existing/transcribed text with AI.

### 2.1 AI reply draft

#### What the user sees

Below the normal reply textarea, show an “AI Draft” button.

When clicked:

- Show a loading state.
- Generate a draft using the selected conversation.
- Display the draft in a small panel.
- Display the provider and model used.
- Provide an “Insert” button.

“Insert” copies the draft into the normal reply textarea, triggers the textarea’s normal input event, and focuses it.

It must not send the reply automatically.

#### Request

```text
POST /admin/ai/crm/conversations/{conversation_id}/draft-reply
```

The source returns an HTML partial because the button uses HTMX. A JSON implementation is also acceptable if it produces the same behaviour.

#### Conversation context sent to the model

Build the context on the server. Include:

- Company name.
- Communication channel from the latest message.
- Conversation status.
- Priority when set.
- Subject.
- Contact display name.
- Assigned agent name.
- Up to eight tags.
- Linked ticket number, title, status, type and priority when available.
- The most recent 12 messages by default, ordered oldest-to-newest.
- Message direction expressed as `customer` or `agent`.

Strip HTML from message bodies, redact sensitive text using the platform’s redaction rules, and limit each message to about 600 characters. Never send raw access tokens, internal credentials or unnecessary customer fields to the model.

#### AI instructions

The model must:

- Act as a customer-support agent for the company.
- Match the communication channel’s tone.
- Reference real details already present in the conversation.
- Avoid mentioning AI or internal systems.
- Avoid inventing facts or promises.
- Ask a clarifying question when information is missing.
- Keep the reply under 120 words.
- Return strict JSON.

Expected structured result:

```json
{
  "draft": "Reply text under 120 words",
  "tone": "professional",
  "clarifying_questions": [],
  "title": "Short title",
  "summary": "What the draft does",
  "confidence": 0.9
}
```

Required keys are `draft`, `tone`, `title` and `summary`.

#### Readiness and storage

- AI must be enabled globally.
- The inbox analyst persona must be enabled.
- Context quality must be at least `0.35`.
- Enforce any configured daily token budget.
- Use the primary LLM endpoint with the configured secondary endpoint as fallback.

Store the generated insight with:

- Persona key `inbox_analyst`.
- Conversation entity ID.
- Structured model output.
- Provider and model.
- Input/output token counts.
- Context-quality score.
- Confidence.
- Generation time.
- Triggering user.
- Expiration time; the source uses 24 hours.

Audit generation without storing the full prompt or conversation context in the audit event.

If AI is disabled, configuration is missing, the budget is exhausted, context is insufficient or the provider fails, show an inline “AI Draft Unavailable” panel. Do not break the normal reply composer.

### 2.2 Voice-to-text

#### Attachment rule

The shared voice script automatically attaches to any:

```html
<textarea data-voice-enabled data-voice-context="crm_reply"></textarea>
```

Use `crm_reply` for the inbox reply composer and `crm_new_conversation` for the new-conversation message.

The page layout must load the shared voice-input script once.

#### What the user sees

Add two controls beside the textarea:

- Microphone button.
- “AI” polish button.

Microphone behaviour:

- Press and hold to record.
- Release to stop.
- Stop automatically after 120 seconds.
- Show recording/transcribing status through an accessible live region.
- Insert the transcript into the textarea without removing existing text.

The implementation should prefer `MediaRecorder` and backend transcription. If recording or backend transcription is unavailable, fall back to the browser’s `SpeechRecognition`/`webkitSpeechRecognition` when supported.

The source also captures browser speech in parallel so it can use that text if backend transcription fails.

#### Supported recording formats

Choose the first browser-supported format from:

```text
audio/webm;codecs=opus
audio/webm
audio/mp4
audio/mpeg
audio/ogg;codecs=opus
audio/ogg
```

#### Transcription request

```text
POST /admin/ai/voice/transcription
Content-Type: multipart/form-data
X-CSRF-Token: {token}

audio={recorded_blob}
context=crm_reply
```

Success:

```json
{
  "ok": true,
  "text": "Transcribed message",
  "meta": {
    "provider": "voice_transcription",
    "model": "gpt-4o-mini-transcribe"
  }
}
```

Failure:

```json
{
  "ok": false,
  "error": "Safe error message",
  "text": ""
}
```

The source returns the failure body with HTTP 200 so the component can handle the error without replacing the page.

#### Transcription backend

- Reject empty audio.
- Reject audio larger than 25 MiB.
- Do not store the audio file.
- Send it to an OpenAI-compatible endpoint:

```text
POST {VOICE_BASE_URL}/v1/audio/transcriptions
Authorization: Bearer {server_side_key}
Content-Type: multipart/form-data

model={configured_model}
response_format=json
file={audio}
```

If the configured base URL already ends in `/v1`, append only `/audio/transcriptions`.

Retry only temporary conditions such as timeouts, connection errors, HTTP `408`, `409`, `425`, `429` and server errors. The source default is one retry with short exponential backoff.

Audit successful transcription with:

- Acting user.
- Context name.
- Audio byte count.
- Content type.
- Provider.
- Model.
- Endpoint.

Do not include the audio itself in the audit record.

### 2.3 AI text polish

Clicking the small “AI” control sends the textarea’s current text:

```text
POST /admin/ai/voice/sentence-suggestion
Content-Type: application/json

{
  "text": "raw or transcribed text",
  "context": "crm_reply"
}
```

Success:

```json
{
  "ok": true,
  "suggested_text": "Cleaned sentence.",
  "alternatives": [
    "Optional alternative."
  ],
  "meta": {
    "provider": "provider name",
    "model": "model name",
    "endpoint": "configured endpoint"
  }
}
```

Rules for polishing:

- Preserve meaning.
- Do not add facts, names, promises, greetings or apologies.
- Use the same language as the input.
- Fix punctuation, spacing, capitalization and obvious grammar.
- Return no more than two distinct alternatives.
- Let the user accept a suggestion; never overwrite or send without user action.

### Required AI configuration

At minimum:

```text
integration.ai_enabled=true
integration.intelligence_inbox_analyst_enabled=true
integration.vllm_base_url={OpenAI-compatible base URL}
integration.vllm_model={model identifier}
integration.vllm_api_key={secret, when required}
```

Voice-specific settings:

```text
integration.voice_transcription_base_url
integration.voice_transcription_model
integration.voice_transcription_api_key
integration.voice_transcription_timeout_seconds
integration.voice_transcription_max_retries
```

Voice base URL and API key may fall back to the primary LLM configuration. The source transcription model default is `gpt-4o-mini-transcribe`.

### Acceptance checks

- AI draft uses the selected conversation, not a generic prompt.
- The returned draft is reviewable and not auto-sent.
- “Insert” updates the real reply textarea and triggers its input behaviour.
- Low-quality or missing conversation data returns a safe unavailable state.
- Voice recording stops on release and at 120 seconds.
- Backend transcription accepts valid audio and rejects empty/oversized audio.
- Browser speech fallback works when backend transcription is unavailable.
- Existing textarea text is preserved when inserting voice text.
- AI polish preserves meaning and requires user acceptance.
- Provider keys never appear in browser requests or HTML.

### Source references

- `templates/admin/crm/_message_thread.html`
- `templates/admin/ai/_conversation_draft_reply.html`
- `templates/admin/ai/_error.html`
- `static/js/voice-input.js`
- `app/web/admin/ai.py`
- `app/services/ai/personas/inbox_analyst.py`
- `app/services/ai/context_builders/inbox.py`
- `app/services/ai/engine.py`
- `app/services/ai/use_cases/voice_transcription.py`
- `app/services/ai/use_cases/voice_sentence_suggestion.py`
- `tests/test_voice_transcription.py`
- `tests/test_voice_sentence_suggestion.py`

---

## 3. Dynamic WhatsApp templates and contact lookup

This functionality belongs to the “Start a new conversation” modal. It is used because a business-initiated WhatsApp conversation must use an approved Meta template.

### Required WhatsApp connector data

Store a WhatsApp connector and an active CRM integration target.

The connector requires:

```text
connector_type = whatsapp
base_url = https://graph.facebook.com/{configured_version}
auth_config.token or auth_config.access_token
metadata.business_account_id or auth_config.business_account_id
metadata.phone_number_id or auth_config.phone_number_id
```

- `business_account_id` is used to list templates.
- `phone_number_id` is used to send messages.
- The access token stays on the server.
- Each selectable “WhatsApp inbox” is an integration target linked to one connector.

### 3.1 Contact lookup

#### What the user sees

When WhatsApp is selected:

- Show a “Contact” search field.
- Start searching after two characters.
- Wait 250 ms after typing before requesting results.
- Show loading, empty and error states.
- Each result shows contact name and WhatsApp number.
- Selecting a result fills the contact name, internal contact ID and phone number.
- Editing the phone number manually clears the selected contact.
- Manual numbers remain allowed.

#### Request

```text
GET /admin/crm/inbox/whatsapp-contacts?search={term}
```

Success:

```json
{
  "contacts": [
    {
      "id": "internal-person-uuid",
      "name": "Customer name",
      "whatsapp_address": "+2348012345678"
    }
  ]
}
```

Return at most 20 results.

#### Server query

- Require inbox-view permission.
- Search active contacts only.
- Include contacts with at least one WhatsApp, phone or SMS channel.
- Match display name, first name, last name, email, main phone or channel address.
- Eager-load channels to avoid one query per result.
- Remove duplicate contacts.
- Sort by display name.

Choose the displayed number in this order:

1. Primary WhatsApp channel.
2. Any WhatsApp channel.
3. Any primary phone/SMS channel.
4. First available phone-like channel.

### Phone normalization

The modal supports these country choices:

```text
NG +234
GH +233
ZA +27
KE +254
GB +44
US +1
```

Normalization rules:

- Keep an existing international `+` number.
- Convert a leading `00` to `+`.
- If the number already begins with the selected calling code, add `+`.
- Otherwise remove one leading local `0` and prepend the selected calling code.
- Default to Nigeria when no country is supplied.

Example:

```text
Country: NG
Input: 08012345678
Stored/sent: +2348012345678
```

If a selected contact has no explicit number in the request, resolve it from the contact’s phone-like channels. If no usable number exists, show “WhatsApp number is required.”

### 3.2 Approved-template list

#### Request

```text
GET /admin/crm/inbox/whatsapp-templates?target_id={integration_target_uuid}
```

Success:

```json
{
  "templates": [
    {
      "name": "welcome_customer",
      "language": "en",
      "status": "APPROVED",
      "category": "UTILITY",
      "components": [],
      "body": "Hello {{1}}"
    }
  ]
}
```

#### Server flow

1. Require inbox-view permission.
2. Resolve the selected active WhatsApp integration target and connector.
3. Read the server-side token and WhatsApp Business Account ID.
4. Request:

```text
GET {base_url}/{business_account_id}/message_templates?limit=200
Authorization: Bearer {token}
```

5. Return template name, language, status, category, full components and body text.
6. Cache results per target/connector for five minutes.
7. In the browser, display only templates whose status is `approved`, case-insensitively.
8. Reload templates whenever the selected WhatsApp inbox changes.

### 3.3 Dynamic template fields

When a template is selected, build fields from its components.

#### Text header

For a `HEADER` with format `TEXT`:

- Find numeric variables such as `{{1}}`, `{{2}}`.
- Remove duplicates.
- Sort numerically.
- Show one required input per variable.

Generated component:

```json
{
  "type": "header",
  "parameters": [
    {"type": "text", "text": "Header value"}
  ]
}
```

#### Media header

For `IMAGE`, `VIDEO` or `DOCUMENT` headers:

- Show one required URL field.
- Generate the matching Meta parameter.

Image example:

```json
{
  "type": "header",
  "parameters": [
    {
      "type": "image",
      "image": {"link": "https://example.com/image.jpg"}
    }
  ]
}
```

Use the equivalent `video` or `document` object for those formats.

#### Body variables

- Read numeric variables from the `BODY` text.
- Remove duplicates and sort numerically.
- Show one required field for each variable.
- Build parameters in that exact numeric order.

```json
{
  "type": "body",
  "parameters": [
    {"type": "text", "text": "First value"},
    {"type": "text", "text": "Second value"}
  ]
}
```

#### Dynamic URL buttons

For each `BUTTONS` entry:

- Only process a button whose type is `URL`.
- Only create an input when its URL contains `{{1}}`.
- Preserve the button’s original zero-based index.

```json
{
  "type": "button",
  "sub_type": "url",
  "index": "0",
  "parameters": [
    {"type": "text", "text": "dynamic-path"}
  ]
}
```

The exact inbox implementation supports numeric template variables. Do not claim support for named variables unless the new platform adds and tests that separately.

### Preview and submitted fields

Replace body tokens with entered values to produce a read-only message preview.

Submit hidden values:

```text
whatsapp_template_name
whatsapp_template_language
whatsapp_template_components
```

`whatsapp_template_components` is the JSON array built from header, body and button inputs.

### Starting the conversation

Request:

```text
POST /admin/crm/inbox/conversation/new
Content-Type: multipart/form-data

channel_type=whatsapp
channel_target_id={target_uuid}
contact_id={optional_contact_uuid}
contact_address=+2348012345678
contact_country_code=NG
contact_name=Customer Name
whatsapp_template_name=welcome_customer
whatsapp_template_language=en
whatsapp_template_components=[...]
message={rendered_preview}
_csrf_token=...
```

Server validation:

- Require inbox-send permission.
- Require a valid channel and target.
- Require template name for new WhatsApp conversations.
- Require template language.
- Require components to parse as a JSON array when supplied.
- Require a valid WhatsApp recipient.
- Reject a missing/malformed template before creating a false sent message.

Contact/conversation behaviour:

- Use the selected contact when a valid contact ID is supplied.
- Otherwise find or create a contact by normalized WhatsApp number.
- Reuse an open WhatsApp conversation when appropriate.
- Create a new conversation when no matching open conversation exists or the selected target differs from the last inbound target.
- Save the selected target as the conversation’s preferred channel target.

### Meta send request

Create a local outbound message in `queued` state before the provider call. Save template details in message metadata.

Provider payload:

```json
{
  "messaging_product": "whatsapp",
  "to": "+2348012345678",
  "type": "template",
  "template": {
    "name": "welcome_customer",
    "language": {
      "code": "en"
    },
    "components": [
      {
        "type": "body",
        "parameters": [
          {"type": "text", "text": "Customer name"}
        ]
      }
    ]
  }
}
```

Send to:

```text
POST {base_url}/{phone_number_id}/messages
Authorization: Bearer {token}
Content-Type: application/json
```

On success:

- Mark the local message `sent`.
- Store Meta’s returned message ID.
- Mark the phone/channel validation state as valid.
- Redirect to the new or reused conversation.

On failure:

- Mark the local message `failed`.
- Store a safe structured provider error internally.
- Mark clearly invalid WhatsApp numbers as invalid when Meta returns the relevant error.
- Retry transient failures up to three total attempts with bounded backoff.
- Do not retry permanent validation/configuration errors.
- Return the user to the inbox with a visible new-conversation error.

### Acceptance checks

- Contact lookup is permission-protected, debounced and limited.
- Selecting a contact fills the real recipient number.
- Manual local numbers normalize correctly for every supported country.
- Changing WhatsApp inbox reloads that inbox’s templates.
- Only approved templates are selectable.
- Header, body and dynamic URL-button inputs create valid Meta component JSON.
- Media headers use a public URL with the correct component type.
- Template name and language are mandatory.
- A successful send stores the provider message ID.
- A failed send remains visible as failed and is never shown as sent.
- Provider tokens never reach the browser.

### Source references

- `templates/admin/crm/inbox.html`
- `app/web/admin/crm_inbox_catalog.py`
- `app/web/admin/crm_inbox_start.py`
- `app/services/crm/inbox/whatsapp_templates.py`
- `app/services/crm/inbox/admin_ui.py`
- `app/services/crm/inbox/outbound.py`
- `app/services/crm/contacts/service.py`
- `app/schemas/crm/inbox.py`
- `tests/test_crm_contacts_whatsapp_search.py`
- `tests/test_crm_inbox_whatsapp_country_numbers.py`

---

## 4. Email CC/BCC delivery

### Exact scope

In the source inbox, CC and BCC fields are part of the “Start a new conversation” email form. They are not currently shown in the existing-conversation reply composer.

Replicate that boundary unless the target platform intentionally expands the feature.

### What the user sees

When the selected new-conversation channel is Email, show:

- Recipient email.
- Subject.
- CC text field.
- BCC text field.
- Message.

Placeholders:

```text
CC: name@company.com, another@company.com
BCC: audit@company.com, manager@company.com
```

Hide CC/BCC for WhatsApp, Facebook and Instagram.

### Submitted form

```text
POST /admin/crm/inbox/conversation/new
Content-Type: multipart/form-data

channel_type=email
channel_target_id={optional_email_target_uuid}
contact_address=customer@example.com
contact_name=Customer Name
subject=Subject
cc_addresses=person1@example.com, person2@example.com
bcc_addresses=audit@example.com; manager@example.com
message=Message text
_csrf_token=...
```

### Parsing and validation

For CC and BCC separately:

1. Accept commas, semicolons and line breaks as separators.
2. Trim whitespace.
3. Remove empty entries.
4. Convert addresses to lowercase.
5. Validate every address.
6. Remove duplicates while preserving first-entry order.
7. Allow at most 20 CC and at most 20 BCC addresses.
8. If any address is invalid, reject the send and identify whether the invalid value came from CC or BCC.

Example:

```text
Input:
USER@example.com; user@example.com
second@example.com

Parsed:
["user@example.com", "second@example.com"]
```

Do not silently discard a malformed address and send to the rest.

### Message persistence

Before sending, create the outbound CRM message with:

- Direction `outbound`.
- Status `queued`.
- Recipient/person channel.
- Selected email integration target.
- Subject.
- Text and HTML body.
- Author.
- Creation/sent attempt time.

Store internal message metadata:

```json
{
  "cc": [
    "person1@example.com"
  ],
  "bcc": [
    "audit@example.com"
  ]
}
```

This metadata is for internal message history and retry/debugging. Protect it with the same permissions as the conversation.

### SMTP construction

Construct the email headers as:

```text
From: Support <support@example.com>
To: customer@example.com
Cc: person1@example.com, person2@example.com
Subject: Subject
```

Never add a `Bcc` header.

Construct the SMTP envelope recipient list as:

```text
[
  primary recipient,
  all CC recipients,
  all BCC recipients
]
```

This is what actually delivers BCC while keeping BCC addresses hidden from recipients.

Pass the same CC/BCC arrays through both send paths:

1. The selected inbox’s connector-specific SMTP configuration.
2. The platform’s default SMTP fallback.

Also preserve existing email reply headers and attachments when applicable:

- `Reply-To`
- `In-Reply-To`
- `References`
- Attachments

### Delivery result

On SMTP acceptance:

- Mark the CRM message `sent`.
- Store SMTP debug/refused-recipient information when returned.

On failure:

- Mark the CRM message `failed`.
- Store a safe error/debug record.
- Retry only transient failures through the normal outbound retry policy.
- Show a visible start-conversation error.
- Never display the message as sent merely because a local record exists.

### Privacy rules

- BCC must never appear in the MIME headers.
- BCC must never be shown to normal recipients.
- Do not include BCC addresses in customer-facing message rendering.
- Internal users may only see CC/BCC metadata if they can access the conversation.
- Do not write full recipient lists into public logs.

### Acceptance checks

- Comma-, semicolon- and newline-separated lists parse correctly.
- Addresses are lowercased and deduplicated.
- Invalid CC and invalid BCC both block sending.
- More than 20 addresses in either list is rejected.
- `Cc` appears in the MIME header.
- `Bcc` does not appear in the MIME header.
- Primary, CC and BCC addresses all appear in the SMTP envelope recipients.
- Connector SMTP and fallback SMTP both receive CC/BCC arrays.
- CC/BCC are stored with the CRM message.
- Failed SMTP delivery marks the message failed.

### Source references

- `templates/admin/crm/inbox.html`
- `app/web/admin/crm_inbox_start.py`
- `app/services/crm/inbox/admin_ui.py`
- `app/services/crm/inbox/outbound.py`
- `app/services/email.py`
- `app/schemas/crm/inbox.py`
- `tests/test_inbox_bcc_support.py`
- `tests/test_crm_inbox_services.py`

---

## Final end-to-end completion checklist

The replacement platform is complete only when all of the following are true:

- The controls call real authenticated server endpoints.
- Server permissions are enforced independently of the UI.
- CSRF protection is active on changing requests.
- Provider secrets remain server-side.
- Provider calls use the correct account/target selected in the inbox.
- Successful provider results are stored with external IDs.
- Failed provider calls are stored as failures and shown to the agent.
- Retries are bounded and limited to temporary failures.
- AI text is always reviewed before sending.
- Voice audio is size-limited and not retained unnecessarily.
- WhatsApp templates are approved, dynamically parameterized and sent as Meta template payloads.
- CC/BCC are actual SMTP envelope recipients, with BCC omitted from email headers.
- Refreshing the page shows stored social replies and sent/failed outbound messages accurately.
