"""Tests for Discord notification sender (tradingagents.notify.discord)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch, call
import json
from pathlib import Path
import pytest

from tradingagents.notify.discord import (
    send_discord,
    format_decision_card,
    format_alert_card,
    format_action_card,
    AlertLevel,
)


# ---------------------------------------------------------------------------
# Test: send_discord basic behavior
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSendDiscord:
    """Tests for the send_discord function."""

    def test_send_discord_not_configured_returns_false(self):
        """When webhook_url is None, should return False and not crash."""
        with patch('tradingagents.notify.discord.logger') as mock_logger:
            result = send_discord("test message", webhook_url=None)
            assert result is False
            mock_logger.warning.assert_called_once()

    def test_send_discord_simple_message_success(self):
        """Send a short message that fits in one chunk."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            mock_response = Mock()
            mock_response.status_code = 204
            mock_post.return_value = mock_response

            result = send_discord("short message", webhook_url="https://discord.com/api/webhooks/123/abc")
            assert result is True
            assert mock_post.call_count == 1

    def test_send_discord_message_2000_chars_exactly_one_chunk(self):
        """Exactly 2000 chars should fit in one message."""
        content = "x" * 2000
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            mock_response = Mock()
            mock_response.status_code = 204
            mock_post.return_value = mock_response

            result = send_discord(content, webhook_url="https://discord.com/api/webhooks/123/abc")
            assert result is True
            assert mock_post.call_count == 1

    def test_send_discord_message_2001_chars_splits_into_two_chunks(self):
        """2001 chars should split into 2 chunks with pagination markers."""
        content = "x" * 2001
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep') as mock_sleep:
                mock_response = Mock()
                mock_response.status_code = 204
                mock_post.return_value = mock_response

                result = send_discord(content, webhook_url="https://discord.com/api/webhooks/123/abc")
                assert result is True
                assert mock_post.call_count == 2
                # Should sleep 1s between chunks
                assert mock_sleep.call_count == 1
                mock_sleep.assert_called_with(1)

    def test_send_discord_catches_all_exceptions(self):
        """Should catch network exceptions and return False without raising."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            mock_post.side_effect = Exception("Network error")

            result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")
            assert result is False


# ---------------------------------------------------------------------------
# Test: Card formatting
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCardFormatting:
    """Tests for markdown card formatting."""

    def test_format_decision_card_basic(self):
        """Basic decision card format."""
        card = format_decision_card(
            symbol="BTC-USD",
            direction="Long",
            leverage=2.0,
            position_size_pct=0.5,
            entry_price=61500,
            stop_loss=60200,
            take_profit=64000,
            cycle="swing_days",
            summary="Strong uptrend with support at 60K",
            risk_gate_status="pass",
            risk_gate_reason=None,
            executor_mode="dryrun",
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        assert "🎯" in card
        assert "BTC-USD" in card
        assert "做多" in card
        assert "2026-08-09 18:00 UTC" in card
        assert "✅" in card

    def test_format_alert_card_warn(self):
        """Alert card for WARN level."""
        card = format_alert_card(
            level=AlertLevel.WARN,
            window_hours=24,
            findings=["Gate rejections exceed threshold: 5 in window (threshold 3)"],
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        assert "⚠️" in card
        assert "WARN" in card
        assert "24h" in card
        assert "Gate rejections" in card
        assert "2026-08-09 18:00 UTC" in card

    def test_format_action_card_closures(self):
        """Action card with position closures."""
        card = format_action_card(
            actions=[
                "平仓 BTCUSDT：交易所持仓与本地状态不一致（naked）",
            ],
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        assert "🛑" in card
        assert "持仓监控动作" in card
        assert "平仓" in card
        assert "2026-08-09 18:00 UTC" in card


# ---------------------------------------------------------------------------
# Test: Deduplication
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAlertDeduplication:
    """Tests for alert deduplication logic."""

    def test_dedup_ttl_not_expired(self, tmp_path):
        """Alert within TTL should not be re-sent."""
        from tradingagents.notify.discord import should_send_alert

        dedup_file = tmp_path / "dedup.json"
        dedup_file.write_text(json.dumps({
            "naked_position:BTCUSDT": datetime.now(timezone.utc).isoformat()
        }))

        result = should_send_alert(
            "naked_position:BTCUSDT",
            dedup_file=dedup_file,
            ttl_hours=6,
        )
        assert result is False

    def test_dedup_ttl_disabled(self, tmp_path):
        """When TTL is 0, should always send."""
        from tradingagents.notify.discord import should_send_alert

        dedup_file = tmp_path / "dedup.json"
        dedup_file.write_text(json.dumps({
            "test_key": datetime.now(timezone.utc).isoformat()
        }))

        result = should_send_alert(
            "test_key",
            dedup_file=dedup_file,
            ttl_hours=0,  # Disabled
        )
        assert result is True


# ---------------------------------------------------------------------------
# Test: fail-open property (hard constraint)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFailOpenProperty:
    """Tests that verify the fail-open isolation property."""

    def test_send_discord_exception_doesnt_raise(self):
        """send_discord must never raise, even on catastrophic errors."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            mock_post.side_effect = RuntimeError("Catastrophic error")

            # Should not raise
            result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")
            assert result is False

    def test_send_discord_returns_bool(self):
        """send_discord always returns a bool, never raises."""
        # Test various failure modes
        test_cases = [
            None,  # Missing webhook
        ]

        for webhook in test_cases:
            result = send_discord("test", webhook_url=webhook)
            assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Test: Format fixtures (prevent regression)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCardFixtures:
    """Tests that card formats match expected fixtures."""

    def test_decision_card_format_fixture(self):
        """Decision card format matches fixture (realistic FuturesDecision
        values: position_size_pct is a decimal fraction, cycle is a
        time_horizon literal rendered in Chinese)."""
        card = format_decision_card(
            symbol="BTC-USD",
            direction="Long",
            leverage=2.0,
            position_size_pct=0.005,
            entry_price=61500,
            stop_loss=60200,
            take_profit=64000,
            cycle="1-3 days",
            summary="Strong uptrend with support at 60K",
            risk_gate_status="pass",
            risk_gate_reason=None,
            executor_mode="dryrun",
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        expected_lines = [
            "🎯 **BTC-USD 决策** · 2026-08-09 18:00 UTC",
            "**方向**: 做多 ｜ **杠杆**: 2.0x ｜ **仓位**: 0.50%",
            "**入场**: 61,500 ｜ **止损**: 60,200 ｜ **止盈**: 64,000",
            "**周期**: 1-3 天",
            "**摘要**: Strong uptrend with support at 60K",
            "**Risk Gate**: ✅ 通过",
            "**执行**: dryrun — 人工下单",
        ]

        actual_lines = card.split("\n")
        # zip() alone would silently pass if trailing lines vanished
        # (mutation check 2026-09-03 proved it) — pin the line count too.
        assert len(actual_lines) == len(expected_lines), (
            f"Line count mismatch: expected {len(expected_lines)}, got {len(actual_lines)}:\n{card}"
        )
        for expected, actual in zip(expected_lines, actual_lines):
            assert expected == actual, f"Mismatch:\n  Expected: {expected}\n  Got:      {actual}"


# ---------------------------------------------------------------------------
# Test: Retry mechanics (required by §6)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSendDiscordRetryMechanics:
    """Comprehensive retry testing per §6."""

    def test_429_retry_with_retry_after_header_sleeps_correct_duration(self):
        """429 with Retry-After header: wait specified seconds, then retry."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep') as mock_sleep:
                response_429 = Mock()
                response_429.status_code = 429
                response_429.headers = {'Retry-After': '0.5'}

                response_204 = Mock()
                response_204.status_code = 204

                mock_post.side_effect = [response_429, response_204]

                result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")
                
                assert result is True
                assert mock_post.call_count == 2
                # Verify sleep was called with correct retry_after value
                mock_sleep.assert_called_once_with(0.5)

    def test_5xx_exponential_backoff_correct_timing(self):
        """5xx errors use 2^attempt seconds: 1s, 2s, 4s, ..."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep') as mock_sleep:
                response_500 = Mock()
                response_500.status_code = 500

                response_204 = Mock()
                response_204.status_code = 204

                # Two 500s, then success
                mock_post.side_effect = [response_500, response_500, response_204]

                result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")
                
                assert result is True
                assert mock_post.call_count == 3
                # First retry: 2^0 = 1s, second retry: 2^1 = 2s
                assert mock_sleep.call_count == 2
                sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
                assert sleep_calls == [1, 2]

    def test_max_3_retries_exhausted_returns_false(self):
        """After 3 retries (4 attempts total), give up and return False."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep'):
                response_500 = Mock()
                response_500.status_code = 500
                mock_post.return_value = response_500

                result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")
                
                assert result is False
                # 1 initial + 3 retries = 4 attempts max
                assert mock_post.call_count == 4

    def test_failed_chunk_does_not_block_subsequent_chunks(self):
        """Multi-chunk: if chunk N fails, chunk N+1 is still attempted."""
        content = "x" * 5000  # Will create 3 chunks
        
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep'):
                response_400 = Mock()
                response_400.status_code = 400
                response_400.text = "Bad request"

                response_204 = Mock()
                response_204.status_code = 204

                # First chunk fails (400), others succeed
                mock_post.side_effect = [response_400, response_204, response_204]

                result = send_discord(content, webhook_url="https://discord.com/api/webhooks/123/abc")
                
                # Overall result is False (one chunk failed)
                assert result is False
                # But all 3 chunks were attempted
                assert mock_post.call_count == 3

    def test_split_at_1999_chars_single_chunk(self):
        """1999 chars should fit in one chunk."""
        content = "x" * 1999
        
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep'):
                response_204 = Mock()
                response_204.status_code = 204
                mock_post.return_value = response_204

                result = send_discord(content, webhook_url="https://discord.com/api/webhooks/123/abc")
                
                assert result is True
                assert mock_post.call_count == 1

    def test_split_at_2000_chars_single_chunk(self):
        """2000 chars should fit in one chunk."""
        content = "x" * 2000
        
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep'):
                response_204 = Mock()
                response_204.status_code = 204
                mock_post.return_value = response_204

                result = send_discord(content, webhook_url="https://discord.com/api/webhooks/123/abc")
                
                assert result is True
                assert mock_post.call_count == 1

    def test_split_at_2001_chars_two_chunks(self):
        """2001 chars should split into 2 chunks."""
        content = "x" * 2001
        
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep') as mock_sleep:
                response_204 = Mock()
                response_204.status_code = 204
                mock_post.return_value = response_204

                result = send_discord(content, webhook_url="https://discord.com/api/webhooks/123/abc")
                
                assert result is True
                assert mock_post.call_count == 2
                # One sleep between chunks
                assert mock_sleep.call_count == 1
                mock_sleep.assert_called_with(1)

    def test_multichunk_payloads_never_exceed_discord_limit(self):
        """The pagination marker is prepended AFTER splitting — a raw
        2000-char chunk plus "(1/3)\\n" would be 2006 chars, and Discord
        rejects content over 2000 with a 400. Assert what actually goes
        over the wire fits, and that no content is lost."""
        content = "x" * 5000
        sent = []

        def capture(url, json=None, timeout=None):
            sent.append(json["content"])
            response = Mock()
            response.status_code = 204
            return response

        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep'):
                mock_post.side_effect = capture
                result = send_discord(content, webhook_url="https://discord.com/api/webhooks/123/abc")

        assert result is True
        assert sent, "nothing was sent"
        oversized = [len(c) for c in sent if len(c) > 2000]
        assert not oversized, f"payloads over Discord's 2000-char limit: {oversized}"
        # Strip the "(i/n)" marker lines and confirm nothing was dropped.
        joined = "".join(
            c.split("\n", 1)[1] if c.startswith("(") else c for c in sent
        )
        assert joined == content

    def test_network_error_retries_with_backoff_then_succeeds(self):
        """requests.RequestException (timeout/DNS — the realistic failure on
        a home network) must retry with 2^attempt backoff, not give up on
        the first blip. Mutation check 2026-09-03: this path had zero
        coverage — removing the retry loop entirely left 110 tests green."""
        import requests as requests_lib

        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep') as mock_sleep:
                response_204 = Mock()
                response_204.status_code = 204

                mock_post.side_effect = [
                    requests_lib.ConnectionError("dns fail"),
                    requests_lib.Timeout("timed out"),
                    response_204,
                ]

                result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")

                assert result is True
                assert mock_post.call_count == 3
                sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
                assert sleep_calls == [1, 2]

    def test_network_error_exhausts_retries_returns_false(self):
        """Persistent network failure: 1 initial + 3 retries, then False."""
        import requests as requests_lib

        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep'):
                mock_post.side_effect = requests_lib.ConnectionError("down")

                result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")

                assert result is False
                assert mock_post.call_count == 4

    def test_invalid_webhook_url_format_returns_false_without_posting(self):
        """Non-Discord URL is rejected before any network call — a typo'd
        webhook must not leak card content to an arbitrary endpoint."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            result = send_discord("test", webhook_url="https://example.com/hook")

            assert result is False
            assert mock_post.call_count == 0

    def test_429_huge_retry_after_is_capped(self):
        """A webhook under a long global limit can answer Retry-After: 3600.
        Honouring it would hang the scheduled run for an hour with no exit
        code and no card — sleep must be capped (OI-N1-3, 2026-09-03)."""
        from tradingagents.notify.discord import _MAX_RETRY_AFTER_S

        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep') as mock_sleep:
                response_429 = Mock()
                response_429.status_code = 429
                response_429.headers = {'Retry-After': '3600'}

                response_204 = Mock()
                response_204.status_code = 204

                mock_post.side_effect = [response_429, response_204]

                result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")

                assert result is True
                mock_sleep.assert_called_once_with(_MAX_RETRY_AFTER_S)

    def test_429_with_json_body_retry_after(self):
        """429 can have retry_after in JSON body."""
        with patch('tradingagents.notify.discord.requests.post') as mock_post:
            with patch('tradingagents.notify.discord.time.sleep') as mock_sleep:
                response_429 = Mock()
                response_429.status_code = 429
                response_429.headers = {}
                response_429.json.return_value = {'retry_after': 0.3}

                response_204 = Mock()
                response_204.status_code = 204

                mock_post.side_effect = [response_429, response_204]

                result = send_discord("test", webhook_url="https://discord.com/api/webhooks/123/abc")
                
                assert result is True
                mock_sleep.assert_called_once_with(0.3)


# ---------------------------------------------------------------------------
# Test: Deduplication edge cases
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAlertDeduplicationEdgeCases:
    """Edge case tests for deduplication logic."""

    def test_dedup_ttl_expired_allows_resend(self, tmp_path):
        """Alert past TTL expiration should be re-sent."""
        from datetime import timedelta, datetime, timezone
        from tradingagents.notify.discord import should_send_alert
        
        dedup_file = tmp_path / "dedup.json"
        # Timestamp 7 hours ago (TTL is 6h)
        past = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        dedup_file.write_text(json.dumps({
            "alert:warn:test_finding": past
        }))

        result = should_send_alert(
            "alert:warn:test_finding",
            dedup_file=dedup_file,
            ttl_hours=6,
        )
        
        assert result is True

    def test_dedup_corrupted_json_fail_open(self, tmp_path):
        """Corrupted JSON in dedup file should allow send (fail-open)."""
        from tradingagents.notify.discord import should_send_alert
        
        dedup_file = tmp_path / "dedup.json"
        dedup_file.write_text("{invalid json [}")

        result = should_send_alert(
            "test_key",
            dedup_file=dedup_file,
            ttl_hours=6,
        )
        
        # Should return True (fail-open) and allow the alert to be sent
        assert result is True


# ---------------------------------------------------------------------------
# Test: Fixture format regression (§6 requirement)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCardFormatFixtures:
    """Card format fixtures prevent template regression (§6)."""

    def test_alert_warn_card_matches_fixture(self):
        """Alert warn card output matches fixture exactly."""
        card = format_alert_card(
            level=AlertLevel.WARN,
            window_hours=24,
            findings=[
                "Gate rejections exceed threshold: 5 in window (threshold 3)",
                "Consecutive stop losses: 3 in sequence",
            ],
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        # Load fixture
        fixture_path = Path(__file__).parent / "fixtures" / "notification_cards" / "alert_warn.md"
        expected = fixture_path.read_text().strip()

        assert card == expected, f"Alert warn card mismatch:\n{card}\nvs\n{expected}"

    def test_alert_critical_card_matches_fixture(self):
        """Alert critical card output matches fixture exactly."""
        card = format_alert_card(
            level=AlertLevel.CRITICAL,
            window_hours=24,
            findings=[
                "Naked position detected: BTCUSDT",
                "Untracked position detected: ETHUSDT",
            ],
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        fixture_path = Path(__file__).parent / "fixtures" / "notification_cards" / "alert_critical.md"
        expected = fixture_path.read_text().strip()

        assert card == expected

    def test_action_closure_card_matches_fixture(self):
        """Action card output matches fixture exactly."""
        card = format_action_card(
            actions=[
                "平仓 BTCUSDT：交易所持仓与本地状态不一致（naked）",
                "采用 ETHUSDT：未跟踪持仓自动记入",
                "检查 5 个持仓，动作 2 个",
            ],
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        fixture_path = Path(__file__).parent / "fixtures" / "notification_cards" / "action_closure.md"
        expected = fixture_path.read_text().strip()

        assert card == expected

    def test_decision_flat_card_matches_fixture(self):
        """Flat decision (1.0x leverage) card output matches fixture."""
        card = format_decision_card(
            symbol="BTC-USD",
            direction="Long",
            leverage=1.0,
            position_size_pct=None,  # Flat: omit position size
            entry_price=61500,
            stop_loss=60200,
            take_profit=64000,
            cycle="flat",
            summary="Consolidating after 10% dip; 4h oversold on RSI",
            risk_gate_status="pass",
            risk_gate_reason=None,
            executor_mode="dryrun",
            timestamp_utc="2026-08-09T18:00:00Z",
        )

        fixture_path = Path(__file__).parent / "fixtures" / "notification_cards" / "decision_flat.md"
        expected = fixture_path.read_text().strip()

        assert card == expected


# ---------------------------------------------------------------------------
# Test: Chinese rendering at the display edge
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestChineseRendering:
    """Values are translated at the card layer only; source strings
    (alerts.py messages, time_horizon literals) stay English for grep."""

    def test_render_finding_zh_known_type_with_count(self):
        from tradingagents.notify.discord import render_finding_zh

        line = render_finding_zh(
            "naked_position", {"count": 2},
            "CRITICAL: Naked position event(s) detected (2). Manual intervention required.",
        )
        assert "2 起裸持仓事件" in line

    def test_render_finding_zh_unknown_type_falls_back_to_message(self):
        from tradingagents.notify.discord import render_finding_zh

        original = "Some new finding the mapping does not know yet"
        assert render_finding_zh("future_type", None, original) == original
        assert render_finding_zh(None, None, original) == original

    def test_render_finding_zh_gate_rejections_includes_breakdown(self):
        from tradingagents.notify.discord import render_finding_zh

        line = render_finding_zh(
            "gate_rejections",
            {"reason_breakdown": {"leverage exceeds configured max_leverage": 3}},
            "Gate rejections exceed threshold: 3 >= 3",
        )
        assert "Risk Gate 拒绝次数超阈值" in line
        assert "×3" in line

    def test_decision_card_translates_time_horizon(self):
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="1-2 weeks", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="dryrun", timestamp_utc="2026-08-18T12:00:00Z",
        )
        assert "**周期**: 1-2 周" in card

    def test_decision_card_unknown_cycle_passes_through(self):
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="whatever", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="dryrun", timestamp_utc="2026-08-18T12:00:00Z",
        )
        assert "**周期**: whatever" in card

    def test_decision_card_translates_side_and_mode(self):
        card = format_decision_card(
            symbol="BTC-USD", direction="Short", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=62800,
            take_profit=58000, cycle="intraday", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="testnet", timestamp_utc="2026-09-01T12:00:00Z",
        )
        assert "**方向**: 做空" in card
        assert "**执行**: testnet — 测试网自动下单" in card

    def test_decision_card_unknown_side_and_mode_pass_through(self):
        card = format_decision_card(
            symbol="BTC-USD", direction="Sideways", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="intraday", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="paper", timestamp_utc="2026-09-01T12:00:00Z",
        )
        assert "**方向**: Sideways" in card
        assert "**执行**: paper" in card

    def test_decision_card_translates_gate_reason(self):
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=5.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="intraday", summary="s",
            risk_gate_status="reject",
            risk_gate_reason="leverage exceeds configured max_leverage",
            executor_mode="dryrun", timestamp_utc="2026-09-01T12:00:00Z",
        )
        assert "🛑 拒绝（杠杆超过配置上限）" in card

    def test_decision_card_unknown_gate_reason_falls_back(self):
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="intraday", summary="s",
            risk_gate_status="reject",
            risk_gate_reason="some brand new reason",
            executor_mode="dryrun", timestamp_utc="2026-09-01T12:00:00Z",
        )
        assert "拒绝（some brand new reason）" in card

    def test_decision_card_absent_values_render_as_dash_not_zero(self):
        """A legitimate no-TP decision must not read as 止盈 0 — absent
        is not a price. Market entry (entry_price=None) renders 市价."""
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=2.0,
            position_size_pct=0.005, entry_price=None, stop_loss=60200,
            take_profit=None, cycle="intraday", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="dryrun", timestamp_utc="2026-09-01T12:00:00Z",
        )
        assert "**入场**: 市价" in card
        assert "**止盈**: —" in card
        assert "止盈**: 0" not in card

    def test_decision_card_includes_truncated_thesis(self):
        thesis = "理由 " * 500  # far over the 700-char cap
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="intraday", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="dryrun", timestamp_utc="2026-09-01T12:00:00Z",
            thesis=thesis,
        )
        thesis_line = next(l for l in card.split("\n") if l.startswith("**依据**"))
        assert thesis_line.endswith("…")
        assert len(thesis_line) < 750
        # And the whole card still fits one Discord message.
        assert len(card) <= 2000

    def test_decision_card_no_thesis_no_line(self):
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="intraday", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="dryrun", timestamp_utc="2026-09-01T12:00:00Z",
        )
        assert "依据" not in card

    def test_decision_card_position_is_decimal_fraction(self):
        """position_size_pct=0.005 (FuturesDecision semantics) → 0.50%,
        not 0.005% — the decimal-vs-percent confusion must not reach
        the approval channel."""
        card = format_decision_card(
            symbol="BTC-USD", direction="Long", leverage=2.0,
            position_size_pct=0.005, entry_price=61500, stop_loss=60200,
            take_profit=64000, cycle="intraday", summary="s",
            risk_gate_status="pass", risk_gate_reason=None,
            executor_mode="dryrun", timestamp_utc="2026-08-18T12:00:00Z",
        )
        assert "**仓位**: 0.50%" in card
        assert "0.005%" not in card
