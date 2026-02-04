"""Tests for email service."""

from unittest.mock import MagicMock, patch

import pytest

from stardag_api.services.email import EmailService


class TestEmailService:
    """Tests for EmailService."""

    @pytest.mark.asyncio
    async def test_email_disabled_returns_false(self):
        """Test that disabled email service returns False without errors."""
        service = EmailService(enabled=False)

        result = await service.send_invite_email(
            to_email="test@example.com",
            workspace_name="Test Workspace",
            inviter_name="John Doe",
            inviter_email="john@example.com",
            role="Member",
            invite_link="https://app.example.com/invites",
            is_new_user=False,
        )

        assert result is False

    def test_formatted_from_address(self):
        """Test formatted from address generation."""
        service = EmailService(
            enabled=True,
            from_address="noreply@example.com",
            from_name="Test App",
        )

        assert service.formatted_from == "Test App <noreply@example.com>"

    @pytest.mark.asyncio
    async def test_send_invite_email_existing_user(self):
        """Test sending invite email to existing user with mocked SES."""
        service = EmailService(
            enabled=True,
            from_address="noreply@example.com",
            from_name="Test App",
            app_url="https://app.example.com",
        )

        # Mock the SES client
        mock_client = MagicMock()
        mock_client.send_email.return_value = {"MessageId": "test-message-id"}
        service._client = mock_client

        result = await service.send_invite_email(
            to_email="recipient@example.com",
            workspace_name="Acme Corp",
            inviter_name="Alice Smith",
            inviter_email="alice@example.com",
            role="Admin",
            invite_link="https://app.example.com/invites/123",
            is_new_user=False,
        )

        assert result is True
        mock_client.send_email.assert_called_once()

        # Verify the call arguments
        call_args = mock_client.send_email.call_args
        assert call_args.kwargs["Source"] == "Test App <noreply@example.com>"
        assert call_args.kwargs["Destination"]["ToAddresses"] == [
            "recipient@example.com"
        ]
        assert "Acme Corp" in call_args.kwargs["Message"]["Subject"]["Data"]
        assert (
            "already have a Stardag account"
            in call_args.kwargs["Message"]["Body"]["Html"]["Data"]
        )

    @pytest.mark.asyncio
    async def test_send_invite_email_new_user(self):
        """Test sending invite email to new user with mocked SES."""
        service = EmailService(
            enabled=True,
            from_address="noreply@example.com",
            from_name="Test App",
            app_url="https://app.example.com",
        )

        mock_client = MagicMock()
        mock_client.send_email.return_value = {"MessageId": "test-message-id"}
        service._client = mock_client

        result = await service.send_invite_email(
            to_email="newuser@example.com",
            workspace_name="Acme Corp",
            inviter_name="Alice Smith",
            inviter_email="alice@example.com",
            role="Member",
            invite_link="https://app.example.com/invites/456",
            is_new_user=True,
        )

        assert result is True

        # Verify new user template is used
        call_args = mock_client.send_email.call_args
        assert (
            "invited you to collaborate"
            in call_args.kwargs["Message"]["Subject"]["Data"]
        )
        assert (
            "Create your free Stardag account"
            in call_args.kwargs["Message"]["Body"]["Html"]["Data"]
        )

    @pytest.mark.asyncio
    async def test_send_email_handles_ses_error(self):
        """Test that SES errors are caught and logged, returning False."""
        service = EmailService(
            enabled=True,
            from_address="noreply@example.com",
            from_name="Test App",
        )

        mock_client = MagicMock()
        mock_client.send_email.side_effect = Exception("SES error: Rate exceeded")
        service._client = mock_client

        result = await service.send_invite_email(
            to_email="recipient@example.com",
            workspace_name="Test Workspace",
            inviter_name="John",
            inviter_email="john@example.com",
            role="Member",
            invite_link="https://app.example.com/invites",
            is_new_user=False,
        )

        # Should return False, not raise
        assert result is False

    @pytest.mark.asyncio
    async def test_template_variables_substituted_correctly(self):
        """Test that all template variables are properly substituted."""
        service = EmailService(
            enabled=True,
            app_url="https://app.test.com",
        )

        mock_client = MagicMock()
        mock_client.send_email.return_value = {"MessageId": "test-id"}
        service._client = mock_client

        await service.send_invite_email(
            to_email="user@example.com",
            workspace_name="My Workspace",
            inviter_name="Jane Doe",
            inviter_email="jane@example.com",
            role="Owner",
            invite_link="https://app.test.com/invite/abc",
            is_new_user=False,
        )

        call_args = mock_client.send_email.call_args
        html_body = call_args.kwargs["Message"]["Body"]["Html"]["Data"]
        text_body = call_args.kwargs["Message"]["Body"]["Text"]["Data"]

        # Check all variables are substituted in HTML
        assert "My Workspace" in html_body
        assert "Jane Doe" in html_body
        assert "jane@example.com" in html_body
        assert "Owner" in html_body
        assert "https://app.test.com/invite/abc" in html_body
        assert "https://app.test.com" in html_body

        # Check no unsubstituted placeholders remain (only checking for single braces
        # since double braces {{ }} are used in CSS and get formatted to single)
        # We check that template variables like {workspace_name} don't remain
        assert "{workspace_name}" not in html_body
        assert "{inviter_name}" not in html_body
        assert "{workspace_name}" not in text_body
        assert "{inviter_name}" not in text_body


class TestEmailServiceIntegration:
    """Integration tests for email in invite flow.

    These tests mock the email service at a higher level to verify
    it's properly integrated into the invite creation flow.
    """

    @pytest.mark.asyncio
    async def test_create_invite_sends_email(self, client):
        """Test that creating an invite triggers email sending."""
        # Create a non-personal workspace via API (user is auto-added as owner)
        create_response = await client.post(
            "/api/v1/ui/workspaces",
            json={"name": "Test Team", "slug": "test-team"},
        )
        assert create_response.status_code == 201
        workspace = create_response.json()

        # Mock the email service where it's imported (top-level import in workspaces)
        with patch(
            "stardag_api.routes.workspaces.get_email_service"
        ) as mock_get_email_service:
            mock_service = MagicMock()
            mock_service.app_url = "https://app.test.com"

            # Make send_invite_email return a coroutine
            async def mock_send(*args, **kwargs):
                return True

            mock_service.send_invite_email = mock_send
            mock_get_email_service.return_value = mock_service

            response = await client.post(
                f"/api/v1/ui/workspaces/{workspace['id']}/invites",
                json={"email": "newmember@example.com", "role": "member"},
            )

            assert response.status_code == 201

            # Verify email service was called
            mock_get_email_service.assert_called_once()

            # Verify invite was created
            data = response.json()
            assert data["email"] == "newmember@example.com"
            assert data["role"] == "member"
