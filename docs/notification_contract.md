# Notification contract

The FastAPI service is the authority for creation, persistence, analytics, and
FCM delivery. Flutter never receives Firebase Admin credentials.

## FCM data payloads

FCM `data` values are always strings. Every new payload includes `type` and a
stable `event_id`; clients use the event ID to suppress repeated navigation.

```json
{
  "type": "announcement",
  "event_id": "announcement document ID",
  "announcement_id": "announcement document ID",
  "title": "Safe display title",
  "body": "Safe display body"
}
```

```json
{
  "type": "knowledge_file",
  "event_id": "knowledge:file-abc",
  "file_id": "file-abc",
  "filename": "student-guide.pdf",
  "title": "ملف معرفي جديد: student-guide.pdf",
  "body": "أصبح ملف student-guide.pdf متاحًا للبحث. افتحه لكتابة سؤال عنه."
}
```

```json
{
  "type": "app_update",
  "event_id": "stable request hash",
  "target_version": "1.2.0",
  "version": "1.3.0",
  "force": "false",
  "title": "Update available",
  "body": "Version 1.3.0 is available."
}
```

Legacy app-update payloads containing `version` and `force` but no `type` are
still recognized by Flutter.

## Topics

- `all_users`: announcements and completed knowledge-file indexing events.
- `version_1_2_0`: app-update targeting for installed version `1.2.0`.

Flutter subscribes only after obtaining an FCM token. It subscribes to the
general topic and to the topic derived from `PackageInfo.version`, subscribing
to the new version topic before removing the previous one.

## HTTP API

- `POST /notifications/announcements` (admin): `{title, body}`. New clients send
  a stable `Idempotency-Key` header for one user action and reuse it after a
  timeout/error. Reusing the key with different content returns `409`.
- `POST /notifications/app-update` (admin):
  `{title, body, target_version, version, force}`.
- `GET /notifications/announcements/latest` (public): exactly the newest three.
- `GET /notifications/announcements/stats` (admin): aggregate counts only.
- `POST /notifications/announcements/{id}/views` (public, optional auth):
  `{installationId}`.

The view endpoint is limited to 20 requests/minute per source IP. The server
validates the installation ID and stores only an HMAC using
`ANALYTICS_ID_PEPPER`. A transaction creates at most one view document and one
counter increment per announcement/install identity. A later authenticated
view on the same installation is reconciled to that count. Installation IDs
are client-generated and therefore estimate installations, not unique humans.
They are format/length validated, HMAC-protected at rest, transactionally
deduplicated per announcement, and the public endpoint is rate limited. These
safeguards reduce accidental and basic automated inflation, but the aggregate
remains an approximate installation-view count rather than device or person
attestation. Firebase App Check is not enforced by this API.

## Firestore

`announcements/{announcementId}` contains `title`, `body`, `type`, `createdAt`,
`createdBy`, `delivery`, and aggregate `viewCount`. Private creator/delivery
metadata is excluded from the public response. Deduplicated view records live
under `announcements/{announcementId}/views/{viewerHmac}` and are never exposed
by the statistics API.

Announcement IDs are deterministically derived from the admin UID and
idempotency key. The delivery state machine is `pending` → transient
`dispatching` → `sent`, `confirmed_pre_dispatch_failure`, or
`delivery_unknown`. Only `confirmed_pre_dispatch_failure` may be dispatched
again with the same key. `delivery_unknown` is quarantined because FCM may have
accepted the request. API errors distinguish `announcement_creation_unknown`,
`push_not_dispatched`, and `push_delivery_unknown`.

`knowledge_file_notification_jobs/{fileId}` durably tracks indexing state,
poll and delivery attempts, lease ownership/expiry, notification state, and the
FCM message ID. A notification is sent only after OpenAI reports `completed`.
Polling workers acquire and renew a Firestore lease, so Railway replicas may
scan the same collection without concurrently processing the same job.

FCM topic delivery is **not exactly once**. The relevant delivery states are:

- `notification_failed`: a confirmed failure before dispatch; safe for the
  bounded recovery path to retry.
- `sending`: dispatch began and the outcome is not yet persisted. A live lease
  is allowed to finish; an expired lease is changed to `delivery_unknown`.
- `delivery_unknown`: FCM may have accepted the send. It is never retried
  automatically.
- `sent`: FCM returned a message ID and that result was persisted.

Operator recovery for `sending` or `delivery_unknown`: inspect the job's
`deliveryDisposition`, `sendStartedAt`, Railway logs, and Firebase delivery
data. The safest action is to leave `delivery_unknown` unchanged. Reset it to
`notification_failed` only when there is external proof no dispatch occurred,
or when the product owner explicitly accepts the risk of a duplicate push.
Startup recovery is best effort with bounded exponential retries and never
blocks the core chat/RAG API from starting.
