"""No-provider regression coverage for logical/native Claude session IDs."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fleet import claude

class NativeSessionIdTests(unittest.TestCase):
    def test_worker_environment_disables_only_inherited_connectors(self):
        with patch.dict(claude.os.environ, {"ENABLE_CLAUDEAI_MCP_SERVERS": "true", "FLEET_TEST_KEEP": "yes"}):
            env = claude.worker_environment()
            self.assertEqual(env["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
            self.assertEqual(env["FLEET_TEST_KEEP"], "yes")
            self.assertEqual(claude.os.environ["ENABLE_CLAUDEAI_MCP_SERVERS"], "true")

    def test_permission_mode_validation_and_explicit_override(self):
        with patch.object(claude.paths, "config", return_value={}):
            self.assertEqual(claude.permission_mode({}), "bypassPermissions")
            self.assertEqual(claude.permission_mode({"permissionMode": "auto"}), "auto")
        with patch.object(claude.paths, "config", return_value={"claudePermissionMode": "default"}):
            self.assertEqual(claude.permission_mode({}), "default")
        for invalid in [None, "", "bypass", [], "--dangerously-skip-permissions"]:
            with self.assertRaises(ValueError):
                claude.validate_permission_mode(invalid)

    def test_uuid_identity_is_unchanged(self):
        sid = "01118b2f-1b90-4864-bdb4-b3e70b2df209"
        self.assertEqual(claude.native_session_id(sid), sid)

    def test_logical_identity_mapping_is_stable_and_distinct(self):
        sid = "pi-deepseek-1215-d532e2a8-0681-487a-a15d-1a88b829292b"
        mapped = claude.native_session_id(sid)
        self.assertEqual(str(uuid.UUID(mapped)), mapped)
        self.assertEqual(mapped, claude.native_session_id(sid))
        self.assertNotEqual(mapped, claude.native_session_id(sid + "-other"))
        with self.assertRaises(ValueError):
            claude.native_session_id("")

    def test_transcript_lookup_uses_native_identity(self):
        sid = "logical-pi-worker"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            file = home / ".claude" / "projects" / "project" / (claude.native_session_id(sid) + ".jsonl")
            file.parent.mkdir(parents=True)
            file.write_text("{}\n")
            with patch.object(claude.Path, "home", return_value=home):
                self.assertEqual(claude.native_transcript(sid), [file])

    def run_mock_turn(self, *, resume=False, wrong_identity=False, allowed_tools=None):
        sid = "pi-deepseek-1215-d532e2a8-0681-487a-a15d-1a88b829292b"
        expected = claude.native_session_id(sid)
        actual = str(uuid.uuid4()) if wrong_identity else expected
        events = [
            {"type":"system", "subtype":"init", "session_id":actual, "model":"test-model"},
            {"type":"result", "subtype":"success", "session_id":actual, "is_error":False},
        ]
        fake = type("Process", (), {"pid":12345, "stdout":io.StringIO("".join(json.dumps(x)+"\n" for x in events)), "wait":lambda self:0})()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.json"
            body = {"sessionId":sid, "cwd":tmp, "model":"test-model", "effort":"xhigh", "resume":resume, "executable":"mock-claude", "message":"bounded test"}
            if allowed_tools is not None:
                body["allowedTools"] = allowed_tools
            request.write_text(json.dumps(body))
            with patch.object(claude.subprocess, "Popen", return_value=fake) as launch, patch.object(claude, "terminate_group") as stop:
                claude.run_turn(request)
                args = launch.call_args.args[0]
                self.assertEqual(launch.call_args.kwargs["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
                mode_index = args.index("--permission-mode")
                self.assertEqual(args[mode_index + 1], "bypassPermissions")
                if allowed_tools:
                    self.assertEqual(args[1:3], ["--allowedTools", ",".join(allowed_tools)])
                    args = [args[0], *args[3:]]
                self.assertEqual(args[1], "--resume" if resume else "--session-id")
                self.assertEqual(args[2], expected)
                state = json.loads((root / "state.json").read_text())
                self.assertEqual(state["sessionId"], sid)
                self.assertEqual(state["nativeSessionId"], expected)
                self.assertEqual(state["permissionMode"], "bypassPermissions")
                self.assertFalse(state["automaticClaudeAiConnectors"])
                self.assertEqual(state["state"], "blocked" if wrong_identity else "idle")
                if wrong_identity:
                    stop.assert_called_once_with(12345)
                else:
                    stop.assert_not_called()
                return state

    def test_new_turn_keeps_logical_state_and_native_cli_identity(self):
        self.assertTrue(self.run_mock_turn()["hasSession"])

    def test_resume_reuses_same_native_uuid(self):
        self.assertTrue(self.run_mock_turn(resume=True)["hasSession"])

    def test_explicit_tool_rules_survive_resume_without_changing_identity(self):
        self.assertTrue(self.run_mock_turn(resume=True, allowed_tools=["Read", "Bash(bun test *)"])["hasSession"])

    def test_mismatched_provider_identity_still_fails_closed(self):
        self.assertFalse(self.run_mock_turn(wrong_identity=True)["hasSession"])
