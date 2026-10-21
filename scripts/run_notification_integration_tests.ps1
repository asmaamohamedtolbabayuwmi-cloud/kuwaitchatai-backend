[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Firebase = "npx"
)

$ErrorActionPreference = "Stop"
$projectId = "demo-kuniv-ai-notifications-test"
$configPath = Join-Path $PSScriptRoot "..\firebase.integration.json"

# Never inherit production-capable credentials into the emulator subprocess.
Remove-Item Env:FIREBASE_SERVICE_ACCOUNT_JSON -ErrorAction SilentlyContinue
Remove-Item Env:GOOGLE_APPLICATION_CREDENTIALS -ErrorAction SilentlyContinue
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
$env:GCLOUD_PROJECT = $projectId
$env:GOOGLE_CLOUD_PROJECT = $projectId
$env:KUNIV_TEST_FIREBASE_PROJECT_ID = $projectId
$env:RUN_NOTIFICATION_INTEGRATION_TESTS = "1"
$env:KUNIV_LOCAL_NOTIFICATION_TEST = "1"
$env:OPENAI_MOCK_MODE = "1"
$env:FIRESTORE_EMULATOR_HOST = "127.0.0.1:48087"
$env:FIREBASE_AUTH_EMULATOR_HOST = "127.0.0.1:49099"
$env:LOCAL_FCM_DELIVERY = "mock"
$env:LOCAL_FCM_CAPTURE_PATH = ".local-test/integration-fcm-capture.jsonl"
$env:ANALYTICS_ID_PEPPER = "notification-integration-test-pepper-only"
# protobuf 4.x's native extension is not compatible with the workstation's
# Python 3.14; the pure-Python implementation is sufficient for emulator I/O.
$env:PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION = "python"

Write-Host "Isolation preflight:"
Write-Host "  Firebase project: $projectId (demo project; live resources disabled)"
Write-Host "  Firestore: Firebase Emulator only (loopback required by test guard)"
Write-Host "  Firebase/OpenAI credentials: removed from child environment"
Write-Host "  FCM/OpenAI: injected fakes plus real-client tripwires"

$testCommand = "$Python -m unittest tests.integration.test_notification_workflow -v"
if ($Firebase -eq "npx") {
    # Firebase CLI 14+ requires Java 21; pin a Java-17-compatible emulator CLI.
    & npm.cmd exec --yes --package=firebase-tools@13.35.1 -- firebase emulators:exec `
        --project $projectId `
        --config $configPath `
        --only auth,firestore `
        $testCommand
} else {
    & $Firebase emulators:exec `
        --project $projectId `
        --config $configPath `
        --only auth,firestore `
        $testCommand
}
$testExitCode = $LASTEXITCODE
Remove-Item (Join-Path $PSScriptRoot "..\firestore-debug.log") -ErrorAction SilentlyContinue
Remove-Item (Join-Path $PSScriptRoot "..\ui-debug.log") -ErrorAction SilentlyContinue
if ($testExitCode -ne 0) {
    throw "Notification integration tests failed with exit code $testExitCode."
}
