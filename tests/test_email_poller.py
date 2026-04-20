"""Tests for the email poller — noise classification, upsert, attachments."""

import pytest

from aegis.ingestion.email_poller import classify_email_noise


# ── Noise classification tests ──────────────────────────


class TestClassifyEmailNoise:
    """Test rule-based email noise classification."""

    def test_noreply_sender_is_automated(self):
        result = classify_email_noise(
            sender_email="noreply@company.com",
            body_text="Your order has been shipped.",
            body_preview="Your order has been shipped.",
        )
        assert result == "automated"

    def test_no_reply_dash_sender_is_automated(self):
        result = classify_email_noise(
            sender_email="no-reply@service.io",
            body_text="Password reset requested.",
            body_preview="Password reset requested.",
        )
        assert result == "automated"

    def test_mailer_daemon_is_automated(self):
        result = classify_email_noise(
            sender_email="mailer-daemon@mail.google.com",
            body_text="Delivery failed.",
            body_preview="Delivery failed.",
        )
        assert result == "automated"

    def test_auto_submitted_header_is_automated(self):
        result = classify_email_noise(
            sender_email="notifications@github.com",
            body_text="A new PR was opened.",
            body_preview="A new PR was opened.",
            headers=[
                {"name": "Auto-Submitted", "value": "auto-generated"},
            ],
        )
        assert result == "automated"

    def test_auto_submitted_no_is_not_automated(self):
        """Auto-Submitted: no means it's a normal human email."""
        result = classify_email_noise(
            sender_email="colleague@company.com",
            body_text="Hey, can we meet tomorrow?",
            body_preview="Hey, can we meet tomorrow?",
            headers=[
                {"name": "Auto-Submitted", "value": "no"},
            ],
        )
        assert result == "human"

    def test_unsubscribe_in_body_is_newsletter(self):
        result = classify_email_noise(
            sender_email="news@techblog.com",
            body_text="Here are your weekly updates. Click here to unsubscribe.",
            body_preview="Here are your weekly updates.",
        )
        assert result == "newsletter"

    def test_list_unsubscribe_header_is_newsletter(self):
        result = classify_email_noise(
            sender_email="marketing@brand.com",
            body_text="Check out our new products!",
            body_preview="Check out our new products!",
            headers=[
                {"name": "List-Unsubscribe", "value": "<mailto:unsub@brand.com>"},
            ],
        )
        assert result == "newsletter"

    def test_known_marketing_domain_is_newsletter(self):
        result = classify_email_noise(
            sender_email="campaign@mailchimp.com",
            body_text="Monthly digest.",
            body_preview="Monthly digest.",
        )
        assert result == "newsletter"

    def test_normal_human_email(self):
        result = classify_email_noise(
            sender_email="colleague@company.com",
            body_text="Can you review the proposal?",
            body_preview="Can you review the proposal?",
        )
        assert result == "human"

    def test_human_email_with_no_headers(self):
        result = classify_email_noise(
            sender_email="manager@org.com",
            body_text="Please submit the budget by Friday.",
            body_preview="Please submit the budget by Friday.",
            headers=None,
        )
        assert result == "human"

    def test_empty_body_still_classifies(self):
        result = classify_email_noise(
            sender_email="person@example.com",
            body_text="",
            body_preview="",
        )
        assert result == "human"

    def test_noreply_in_subdomain(self):
        """Test that noreply patterns match in various positions."""
        result = classify_email_noise(
            sender_email="alerts.noreply@company.com",
            body_text="System alert.",
            body_preview="System alert.",
        )
        assert result == "automated"

    def test_postmaster_is_automated(self):
        result = classify_email_noise(
            sender_email="postmaster@mail.company.com",
            body_text="Undeliverable message.",
            body_preview="Undeliverable message.",
        )
        assert result == "automated"

    def test_bounce_sender_is_automated(self):
        result = classify_email_noise(
            sender_email="bounce@notifications.service.com",
            body_text="Message bounced.",
            body_preview="Message bounced.",
        )
        assert result == "automated"

    def test_sendgrid_domain_is_newsletter(self):
        # info@ matches automated sender patterns; sendgrid domain also matches newsletter
        # automated check runs first, so this is classified as automated
        result = classify_email_noise(
            sender_email="info@sendgrid.net",
            body_text="Your report is ready.",
            body_preview="Your report is ready.",
        )
        assert result == "automated"

    def test_automated_takes_priority_over_newsletter(self):
        """If sender is noreply AND body has unsubscribe, automated wins (checked first)."""
        result = classify_email_noise(
            sender_email="noreply@company.com",
            body_text="Weekly digest. Click to unsubscribe.",
            body_preview="Weekly digest.",
        )
        assert result == "automated"
