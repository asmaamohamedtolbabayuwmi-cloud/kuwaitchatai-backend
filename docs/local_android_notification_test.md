# Safe local Admin-to-Android notification test

This environment keeps Auth, Firestore, announcement history, analytics, and
knowledge notification jobs in local emulators. OpenAI upload/indexing is an
in-memory simulation. FCM defaults to a JSONL capture and cannot send a real
message.

## Preparation mode (no real FCM)

Use three PowerShell windows.

### 1. Start Firebase emulators

```powershell
cd F:\kuwaitchatai-backend
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_firebase_emulators.ps1
```

### 2. Start the backend in capture-only mode

```powershell
cd F:\kuwaitchatai-backend
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_notification_backend.ps1
```

The startup aborts if a Firebase/OpenAI credential is present, the emulator is
not loopback, the project is production, or OpenAI mocking is disabled.
Captured notification envelopes go to `.local-test/fcm-capture.jsonl`. They
contain no token, topic, condition, or token list.

### 3. Connect one USB Android device and run Flutter

```powershell
adb devices
adb reverse tcp:8000 tcp:8000
adb reverse tcp:48087 tcp:48087
adb reverse tcp:49099 tcp:49099
cd F:\kuwaitchatai_web
flutter run -d <ANDROID_DEVICE_ID> --debug `
  --dart-define=LOCAL_NOTIFICATION_TEST=true `
  --dart-define=USE_LOCAL_BACKEND=true `
  --dart-define=LOCAL_FIREBASE_PROJECT_ID=demo-kuniv-ai-notifications-test `
  --dart-define=LOCAL_FIREBASE_API_KEY=local-emulator-only `
  --dart-define=LOCAL_FIREBASE_APP_ID=1:123456789:android:localtest `
  --dart-define=LOCAL_FIREBASE_MESSAGING_SENDER_ID=123456789
```

`adb reverse` is required for the backend, Firestore emulator, and Auth
emulator because the Android app uses `127.0.0.1`. This preparation command
uses intentionally non-live Firebase identifiers: the Admin forms and backend
workflow work, but no usable FCM registration token is created.

The app signs into the Auth emulator anonymously and creates only its own
emulator user document with `role=admin`. That identity path is gated by both a
debug build check and the `LOCAL_NOTIFICATION_TEST` compile-time define.

## Automated checks

```powershell
cd F:\kuwaitchatai-backend
.\.venv\Scripts\python.exe -m unittest tests.test_local_notification_safety tests.test_fcm_device_cli tests.test_notifications -v
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_notification_integration_tests.ps1 -Python .\.venv\Scripts\python.exe

cd F:\kuwaitchatai_web
flutter test test\local_notification_test_config_test.dart test\admin_notification_form_test.dart test\admin_notification_routing_test.dart test\admin_notification_viewmodel_test.dart test\admin_upload_viewmodel_test.dart test\app_config_test.dart test\chat_notification_draft_test.dart test\notification_navigation_test.dart test\push_notification_service_test.dart test\latest_updates_screen_test.dart
```

## Enabling one real device after explicit approval

Do not perform this section until the owner explicitly approves a real send.

1. Create/use a dedicated **non-production Firebase project** and register the
   debug Android app `chat.kuwaitchatai.app`. Never use
   `kuwaitchat-ai-project`.
2. Use a dedicated service account in that test project with only the
   `Firebase Cloud Messaging API Admin` role. Do not grant Firestore/Datastore
   roles. Keep its JSON file outside both repositories.
3. Run Flutter with that test project's API key, app ID, sender ID, and project
   ID. Add `--dart-define=LOG_FCM_TEST_TOKEN=true` only for this run. Copy the
   printed token privately; the app never stores it. Topic subscription is
   disabled in local test mode.
4. Generate a bridge secret in PowerShell and keep it only in the two process
   environments:

```powershell
$bridgeSecret = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

5. Start the isolated bridge. It prompts for the one token using hidden input
   and requires an exact device/project confirmation:

```powershell
cd F:\kuwaitchatai-backend
.\.venv\Scripts\python.exe -m local_tools.fcm_single_device_bridge `
  --credential C:\secure\kuniv-notification-test-fcm-only.json `
  --project-id <TEST_FIREBASE_PROJECT_ID> `
  --device-label "My USB Android" `
  --secret $bridgeSecret
```

6. Restart both emulators and the backend with the same test project. Bridge
   delivery also requires an explicit approval phrase:

```powershell
cd F:\kuwaitchatai-backend
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_firebase_emulators.ps1 `
  -FirebaseProjectId <TEST_FIREBASE_PROJECT_ID>

$env:ALLOW_REAL_SINGLE_DEVICE_FCM = "I_APPROVE_ONE_DEVICE"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_notification_backend.ps1 `
  -Delivery bridge `
  -FirebaseProjectId <TEST_FIREBASE_PROJECT_ID> `
  -BridgeUrl http://127.0.0.1:48787/send `
  -BridgeSecret $bridgeSecret
```

The FastAPI process still has no Firebase credential or FCM token. It sends a
typed payload over loopback to the bridge. The bridge constructs only
`messaging.Message(token=...)`; topic, condition, and multicast fields are
rejected.

## Manual Android workflows after approval

1. Open the real Admin Dashboard and choose **Notifications**.
2. Send an announcement. Tap the Android notification and confirm **Latest
   Updates** opens and shows the announcement. Return and confirm the Admin
   view count updates from emulator analytics.
3. Send an app update where the new version is greater than the installed
   target version. Tap it and confirm the optional update dialog appears and
   can be dismissed when `force` is off.
4. In **Upload**, choose a small PDF. Confirm the UI reports upload success and
   indexing in progress. Wait for the mock's second indexing poll, then tap the
   `knowledge_file` notification. Confirm ChatScreen opens with a question
   about that filename prefilled.
5. Before step 4, type a non-empty chat draft. Tap the knowledge notification
   and confirm the existing draft remains unchanged.
6. Repeat from foreground, background, and terminated states, checking Back
   navigation after each notification.

Stop the bridge and unset `ALLOW_REAL_SINGLE_DEVICE_FCM` after testing. Remove
the `LOG_FCM_TEST_TOKEN` define from later runs.

## Task-related changed-file manifest

Flutter (`F:\kuwaitchatai_web`):

- `android/app/src/debug/AndroidManifest.xml`
- `android/app/src/debug/res/xml/network_security_config.xml`
- `lib/core/config/app_config.dart`
- `lib/core/config/local_notification_test_config.dart`
- `lib/core/config/remote_config_keys.dart`
- `lib/core/config/remote_config_service.dart`
- `lib/core/services/chat_draft_coordinator.dart`
- `lib/core/services/debug_fcm_token_logger.dart`
- `lib/core/services/notification_navigation.dart`
- `lib/core/services/push_notification_service.dart`
- `lib/feature/auth/presentation/pages/login_screen.dart`
- `lib/feature/chat/data/datasources/admin_remote_data_source.dart`
- `lib/feature/chat/data/datasources/chat_remote_ds.dart`
- `lib/feature/chat/data/models/knowledge_upload_result.dart`
- `lib/feature/chat/data/repositories/admin_repository_impl.dart`
- `lib/feature/chat/domain/repositories/admin_repository.dart`
- `lib/feature/chat/domain/usecases/send_notification_usecase.dart`
- `lib/feature/chat/domain/usecases/upload_file_usecase.dart`
- `lib/feature/chat/presentation/pages/admin_notification_screen.dart`
- `lib/feature/chat/presentation/pages/admin_upload_screen.dart`
- `lib/feature/chat/presentation/pages/chat_screen.dart`
- `lib/feature/chat/presentation/viewmodel/admin_notification_viewmodel.dart`
- `lib/feature/chat/presentation/viewmodel/admin_upload_viewmodel.dart`
- `lib/feature/notifications/data/notification_api_client.dart`
- `lib/feature/notifications/presentation/latest_updates_screen.dart`
- `lib/feature/schedule/data/datasources/schedule_remote_data_source.dart`
- `lib/main.dart`
- `test/admin_notification_form_test.dart`
- `test/admin_notification_routing_test.dart`
- `test/admin_notification_viewmodel_test.dart`
- `test/admin_remote_data_source_test.dart`
- `test/admin_upload_viewmodel_test.dart`
- `test/app_config_test.dart`
- `test/chat_notification_draft_test.dart`
- `test/debug_fcm_token_logger_test.dart`
- `test/latest_updates_screen_test.dart`
- `test/local_notification_test_config_test.dart`
- `test/localized_error_test.dart`
- `test/notification_navigation_test.dart`
- `test/push_notification_service_test.dart`
- `test/signed_in_bootstrap_test.dart`
- `test/startup_test.dart`

Backend (`F:\kuwaitchatai-backend`):

- `.dockerignore`
- `.gitignore`
- `app/auth.py`
- `app/config.py`
- `app/firebase.py`
- `app/local_notification_test.py`
- `app/local_openai_mock.py`
- `app/openai_client.py`
- `app/routers/__init__.py`
- `app/routers/notifications.py`
- `app/routers/upload.py`
- `app/services/knowledge_file_notifications.py`
- `app/services/notification_service.py`
- `app/vector_store.py`
- `docs/local_android_notification_test.md`
- `docs/notification_contract.md`
- `firebase.integration.json`
- `local_tools/fcm_device_test.py`
- `local_tools/fcm_single_device_bridge.py`
- `main.py`
- `scripts/run_notification_integration_tests.ps1`
- `scripts/start_local_firebase_emulators.ps1`
- `scripts/start_local_notification_backend.ps1`
- `tests/integration/__init__.py`
- `tests/integration/firestore.rules`
- `tests/integration/test_notification_workflow.py`
- `tests/test_fcm_device_cli.py`
- `tests/test_local_notification_safety.py`
- `tests/test_notifications.py`

The repositories were already dirty when this work was audited. Unrelated
generated files, reports, and scratch files are intentionally excluded from
this task manifest and were not cleaned up.
