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
        assert "Long" in card
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
        """Decision card format matches fixture."""
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

        expected_lines = [
            "🎯 **BTC-USD 决策** · 2026-08-09 18:00 UTC",
            "**方向**: Long ｜ **杠杆**: 2.0x ｜ **仓位**: 0.5%",
            "**入场**: 61,500 ｜ **止损**: 60,200 ｜ **止盈**: 64,000",
            "**周期**: swing_days",
            "**摘要**: Strong uptrend with support at 60K",
            "**Risk Gate**: ✅ 通过",
            "**执行**: dryrun",
        ]

        actual_lines = card.split("\n")
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
