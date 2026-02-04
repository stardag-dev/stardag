"""Email service for sending transactional emails via AWS SES.

This module provides email functionality for workspace invitations and
other transactional emails.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# HTML email templates
INVITE_EXISTING_USER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .button {{ display: inline-block; padding: 12px 24px; background-color: #0066cc; color: white; text-decoration: none; border-radius: 6px; }}
        .footer {{ margin-top: 40px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <p>Hi,</p>
        <p><strong>{inviter_name}</strong> ({inviter_email}) has invited you to join <strong>{workspace_name}</strong> on Stardag as a <strong>{role}</strong>.</p>
        <p>Since you already have a Stardag account, you can accept this invitation directly from your dashboard.</p>
        <p><a href="{invite_link}" class="button">Accept Invitation</a></p>
        <p>Or log in at <a href="{app_url}">{app_url}</a> and navigate to your pending invitations.</p>
        <p>If you weren't expecting this invitation, you can safely ignore this email.</p>
        <p>Best,<br>The Stardag Team</p>
        <div class="footer">
            <p>This email was sent by Stardag. If you have questions, contact us at support@stardag.com.</p>
        </div>
    </div>
</body>
</html>
"""

INVITE_EXISTING_USER_TEXT = """
Hi,

{inviter_name} ({inviter_email}) has invited you to join {workspace_name} on Stardag as a {role}.

Since you already have a Stardag account, you can accept this invitation directly from your dashboard.

Accept Invitation: {invite_link}

Or log in at {app_url} and navigate to your pending invitations.

If you weren't expecting this invitation, you can safely ignore this email.

Best,
The Stardag Team
"""

INVITE_NEW_USER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .button {{ display: inline-block; padding: 12px 24px; background-color: #0066cc; color: white; text-decoration: none; border-radius: 6px; }}
        .footer {{ margin-top: 40px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <p>Hi,</p>
        <p><strong>{inviter_name}</strong> ({inviter_email}) has invited you to join <strong>{workspace_name}</strong> on Stardag as a <strong>{role}</strong>.</p>
        <p>Stardag is a declarative DAG framework for Python that helps teams build reproducible data pipelines with persistent asset management.</p>
        <p>To get started:</p>
        <ol>
            <li>Create your free Stardag account</li>
            <li>Your invitation to {workspace_name} will be waiting for you</li>
        </ol>
        <p><a href="{invite_link}" class="button">Create Account &amp; Accept Invitation</a></p>
        <p>If you weren't expecting this invitation, you can safely ignore this email.</p>
        <p>Best,<br>The Stardag Team</p>
        <div class="footer">
            <p>This email was sent by Stardag. If you have questions, contact us at support@stardag.com.</p>
        </div>
    </div>
</body>
</html>
"""

INVITE_NEW_USER_TEXT = """
Hi,

{inviter_name} ({inviter_email}) has invited you to join {workspace_name} on Stardag as a {role}.

Stardag is a declarative DAG framework for Python that helps teams build reproducible data pipelines with persistent asset management.

To get started:
1. Create your free Stardag account
2. Your invitation to {workspace_name} will be waiting for you

Create Account & Accept Invitation: {invite_link}

If you weren't expecting this invitation, you can safely ignore this email.

Best,
The Stardag Team
"""


class EmailService:
    """Service for sending transactional emails via AWS SES."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        from_address: str = "noreply@stardag.com",
        from_name: str = "Stardag",
        region: str = "us-east-1",
        app_url: str = "https://app.stardag.com",
    ):
        self.enabled = enabled
        self.from_address = from_address
        self.from_name = from_name
        self.region = region
        self.app_url = app_url
        self._client: Any = None

    def _get_client(self) -> Any:
        """Get or create the SES client (lazy initialization)."""
        if self._client is None:
            try:
                import boto3

                self._client = boto3.client("ses", region_name=self.region)
            except ImportError:
                logger.warning("boto3 not installed, email sending disabled")
                self.enabled = False
                return None
        return self._client

    @property
    def formatted_from(self) -> str:
        """Get formatted From address."""
        return f"{self.from_name} <{self.from_address}>"

    async def send_invite_email(
        self,
        *,
        to_email: str,
        workspace_name: str,
        inviter_name: str,
        inviter_email: str,
        role: str,
        invite_link: str,
        is_new_user: bool,
    ) -> bool:
        """Send workspace invitation email.

        Args:
            to_email: Recipient email address
            workspace_name: Name of the workspace
            inviter_name: Display name of the person who sent the invite
            inviter_email: Email of the person who sent the invite
            role: Role being granted (e.g., "Member", "Admin")
            invite_link: Direct link to accept the invitation
            is_new_user: True if recipient needs to create an account first

        Returns:
            True if sent successfully, False otherwise.
            Does not raise exceptions - email failures should not block invites.
        """
        if not self.enabled:
            logger.info("Email disabled, skipping invite email to %s", to_email)
            return False

        # Select template based on user status
        if is_new_user:
            subject = f"{inviter_name} invited you to collaborate on Stardag"
            html_template = INVITE_NEW_USER_HTML
            text_template = INVITE_NEW_USER_TEXT
        else:
            subject = f"You've been invited to join {workspace_name} on Stardag"
            html_template = INVITE_EXISTING_USER_HTML
            text_template = INVITE_EXISTING_USER_TEXT

        # Format templates
        template_vars = {
            "workspace_name": workspace_name,
            "inviter_name": inviter_name,
            "inviter_email": inviter_email,
            "role": role,
            "invite_link": invite_link,
            "app_url": self.app_url,
        }
        html_body = html_template.format(**template_vars)
        text_body = text_template.format(**template_vars)

        return await self._send_email(
            to_email=to_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )

    async def _send_email(
        self,
        *,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: str,
    ) -> bool:
        """Send an email via SES.

        Returns True if sent successfully, False otherwise.
        """
        client = self._get_client()
        if client is None:
            return False

        try:
            response = client.send_email(
                Source=self.formatted_from,
                Destination={"ToAddresses": [to_email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": text_body, "Charset": "UTF-8"},
                        "Html": {"Data": html_body, "Charset": "UTF-8"},
                    },
                },
            )
            message_id = response.get("MessageId", "unknown")
            logger.info(
                "Sent email to %s, subject=%r, message_id=%s",
                to_email,
                subject,
                message_id,
            )
            return True
        except Exception as e:
            logger.error("Failed to send email to %s: %s", to_email, e)
            return False


# Global instance (configured from settings)
_email_service: EmailService | None = None


def get_email_service() -> EmailService:
    """Get the global email service instance."""
    global _email_service
    if _email_service is None:
        from stardag_api.config import email_settings

        _email_service = EmailService(
            enabled=email_settings.enabled,
            from_address=email_settings.from_address,
            from_name=email_settings.from_name,
            region=email_settings.ses_region,
            app_url=email_settings.app_url,
        )
    return _email_service
