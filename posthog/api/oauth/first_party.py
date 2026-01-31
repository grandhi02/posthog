"""
First-party OAuth token endpoint for Twig and other PostHog applications.

This module provides endpoints that allow first-party PostHog applications to:
1. Exchange email/password directly for OAuth tokens (no browser redirect)
2. Complete the OAuth flow after social authentication

First-party applications are trusted apps built by PostHog (like Twig) that
skip the OAuth consent screen and can use a simplified authentication flow.
"""

import time
import uuid
import secrets
from datetime import timedelta
from typing import Any, Optional, cast

from django.contrib.auth import authenticate
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

import structlog
from axes.handlers.proxy import AxesProxyHandler
from django_otp.plugins.otp_static.models import StaticDevice
from oauth2_provider.settings import oauth2_settings
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from social_django.strategy import DjangoStrategy
from two_factor.utils import default_device

from posthog.api.email_verification import EmailVerifier, is_email_verification_disabled
from posthog.email import is_email_available
from posthog.helpers.two_factor_session import has_passkeys
from posthog.models import OAuthApplication, OrganizationDomain, Team, User
from posthog.models.oauth import OAuthAccessToken, OAuthGrant, OAuthRefreshToken
from posthog.models.utils import generate_random_oauth_access_token, generate_random_oauth_refresh_token
from posthog.rate_limit import FirstPartyTokenThrottle
from posthog.user_permissions import UserPermissions

logger = structlog.get_logger(__name__)

# Session timeout for 2FA (10 minutes)
FIRST_PARTY_2FA_TIMEOUT = 600


class FirstPartyTokenSerializer(serializers.Serializer):
    """Serializer for first-party token exchange requests."""

    client_id = serializers.CharField()
    email = serializers.EmailField()
    password = serializers.CharField()
    code_verifier = serializers.CharField()
    scope = serializers.CharField(required=False, default="")
    scoped_teams = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)


class FirstPartyTwoFactorSerializer(serializers.Serializer):
    """Serializer for first-party 2FA completion."""

    client_id = serializers.CharField()
    session_token = serializers.CharField()
    code = serializers.CharField()
    code_verifier = serializers.CharField()


@method_decorator(csrf_exempt, name="dispatch")
class FirstPartyTokenView(APIView):
    """
    Direct token exchange for first-party apps.

    POST /oauth/first-party-token/

    Request:
    {
        "client_id": "...",
        "email": "user@example.com",
        "password": "...",
        "code_verifier": "...",
        "scope": "user:read project:read ...",
        "scoped_teams": [123]
    }

    Response (success):
    {
        "access_token": "...",
        "refresh_token": "...",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "...",
        "scoped_teams": [123],
        "scoped_organizations": []
    }

    Response (2FA required):
    {
        "requires_2fa": true,
        "2fa_methods": ["totp", "backup_codes"],
        "session_token": "..."
    }
    """

    permission_classes = [AllowAny]
    throttle_classes = [FirstPartyTokenThrottle]

    def post(self, request: Request, *args, **kwargs) -> Response:
        serializer = FirstPartyTokenSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        # Check if this IP/user is locked out due to too many failed attempts
        if AxesProxyHandler.is_locked(request, credentials={"username": data["email"]}):
            return Response(
                {
                    "error": "too_many_attempts",
                    "error_description": "Too many failed login attempts. Please try again later.",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Validate first-party application
        try:
            application = OAuthApplication.objects.get(client_id=data["client_id"])
        except OAuthApplication.DoesNotExist:
            return Response(
                {"error": "invalid_client", "error_description": "Unknown client"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not application.is_first_party:
            return Response(
                {
                    "error": "unauthorized_client",
                    "error_description": "This endpoint is only available for first-party applications",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check SSO enforcement
        sso_enforcement = OrganizationDomain.objects.get_sso_enforcement_for_email_address(data["email"])
        if sso_enforcement:
            return Response(
                {
                    "error": "sso_required",
                    "error_description": f"SSO login is required for this account ({sso_enforcement})",
                    "sso_provider": sso_enforcement,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Authenticate user
        user = cast(
            Optional[User],
            authenticate(request, email=data["email"], password=data["password"]),
        )

        if not user:
            # Record failed login attempt for axes
            AxesProxyHandler.user_login_failed(
                sender=self.__class__,
                credentials={"username": data["email"]},
                request=request,
            )
            return Response(
                {"error": "invalid_credentials", "error_description": "Invalid email or password"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Check email verification
        if is_email_available() and user.is_email_verified is False and not is_email_verification_disabled(user):
            EmailVerifier.create_token_and_send_email_verification(user)
            return Response(
                {
                    "error": "email_not_verified",
                    "error_description": "Your account is awaiting verification. Please check your email.",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Check 2FA requirement
        two_fa_methods = self._get_2fa_methods(user)
        if two_fa_methods:
            # Create session token for 2FA completion
            session_token = self._create_2fa_session(user, data, application)
            return Response(
                {
                    "requires_2fa": True,
                    "2fa_methods": two_fa_methods,
                    "session_token": session_token,
                }
            )

        # No 2FA required - issue tokens directly
        return self._issue_tokens(user, application, data)

    def _get_2fa_methods(self, user: User) -> list[str]:
        """Get available 2FA methods for the user."""
        methods = []

        # Check TOTP device
        totp_device = default_device(user)
        if totp_device:
            methods.append("totp")
            methods.append("backup_codes")

        # Check passkeys enabled for 2FA
        if has_passkeys(user) and user.passkeys_enabled_for_2fa:
            methods.append("passkey")

        return methods

    def _create_2fa_session(self, user: User, data: dict[str, Any], application: OAuthApplication) -> str:
        """Create a session token for 2FA completion."""
        session_token = secrets.token_urlsafe(32)
        cache_key = f"first_party_2fa:{session_token}"
        cache.set(
            cache_key,
            {
                "user_id": user.pk,
                "client_id": data["client_id"],
                "code_verifier": data["code_verifier"],
                "scope": data.get("scope", ""),
                "scoped_teams": data.get("scoped_teams", []),
                "created_at": time.time(),
            },
            timeout=FIRST_PARTY_2FA_TIMEOUT,
        )

        return session_token

    def _issue_tokens(
        self,
        user: User,
        application: OAuthApplication,
        data: dict[str, Any],
    ) -> Response:
        """Issue OAuth tokens for the authenticated user."""
        # Validate scoped teams
        scoped_teams = data.get("scoped_teams", [])
        user_permissions = UserPermissions(user)

        if scoped_teams:
            for team_id in scoped_teams:
                try:
                    team = Team.objects.get(pk=team_id)
                    if user_permissions.team(team).effective_membership_level is None:
                        return Response(
                            {
                                "error": "invalid_scope",
                                "error_description": f"You do not have access to team {team_id}",
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                except Team.DoesNotExist:
                    return Response(
                        {
                            "error": "invalid_scope",
                            "error_description": f"Team {team_id} does not exist",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        else:
            # Default to all accessible teams
            scoped_teams = list(Team.objects.filter(organization__members=user).values_list("pk", flat=True))

        scope = data.get("scope", "")
        expires_in = cast(int, oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS)
        expires = timezone.now() + timedelta(seconds=expires_in)

        access_token_value = generate_random_oauth_access_token(None)
        refresh_token_value = generate_random_oauth_refresh_token(None)

        # Create access token
        access_token = OAuthAccessToken.objects.create(
            user=user,
            application=application,
            token=access_token_value,
            expires=expires,
            scope=scope,
            scoped_teams=scoped_teams,
            scoped_organizations=[],
        )

        # Create refresh token
        OAuthRefreshToken.objects.create(
            user=user,
            application=application,
            access_token=access_token,
            token=refresh_token_value,
            token_family=uuid.uuid4(),
            scoped_teams=scoped_teams,
            scoped_organizations=[],
        )

        return Response(
            {
                "access_token": access_token_value,
                "refresh_token": refresh_token_value,
                "token_type": "Bearer",
                "expires_in": expires_in,
                "scope": scope,
                "scoped_teams": scoped_teams,
                "scoped_organizations": [],
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class FirstPartyTwoFactorView(APIView):
    """
    Complete 2FA for first-party token exchange.

    POST /api/oauth/first-party-token/2fa/

    Request:
    {
        "client_id": "...",
        "session_token": "...",
        "code": "123456",
        "code_verifier": "..."
    }
    """

    permission_classes = [AllowAny]
    throttle_classes = [FirstPartyTokenThrottle]

    def post(self, request: Request, *args, **kwargs) -> Response:
        serializer = FirstPartyTwoFactorSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        cache_key = f"first_party_2fa:{data['session_token']}"
        session_data = cache.get(cache_key)

        if not session_data:
            return Response(
                {"error": "invalid_session", "error_description": "Session expired or invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate session hasn't expired
        if time.time() - session_data["created_at"] > FIRST_PARTY_2FA_TIMEOUT:
            cache.delete(cache_key)
            return Response(
                {"error": "session_expired", "error_description": "2FA session expired"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate client_id matches
        if session_data["client_id"] != data["client_id"]:
            return Response(
                {"error": "invalid_client", "error_description": "Client ID mismatch"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate code_verifier matches
        if session_data["code_verifier"] != data["code_verifier"]:
            return Response(
                {"error": "invalid_request", "error_description": "Code verifier mismatch"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get user and application
        try:
            user = User.objects.get(pk=session_data["user_id"])
            application = OAuthApplication.objects.get(client_id=data["client_id"])
        except (User.DoesNotExist, OAuthApplication.DoesNotExist):
            cache.delete(cache_key)
            return Response(
                {"error": "invalid_session", "error_description": "Invalid session data"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify 2FA code
        if not self._verify_2fa_code(user, data["code"]):
            return Response(
                {"error": "invalid_code", "error_description": "Invalid 2FA code"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Clean up session
        cache.delete(cache_key)

        # Issue tokens
        return FirstPartyTokenView()._issue_tokens(
            user,
            application,
            {
                "scope": session_data.get("scope", ""),
                "scoped_teams": session_data.get("scoped_teams", []),
            },
        )

    def _verify_2fa_code(self, user: User, code: str) -> bool:
        """Verify a 2FA code (TOTP or backup code)."""
        verified = False

        # Check TOTP device
        totp_device = default_device(user)
        if totp_device:
            is_allowed = totp_device.verify_is_allowed()
            if is_allowed[0] and totp_device.verify_token(code):
                totp_device.throttle_reset()
                verified = True
            else:
                totp_device.throttle_increment()

        # Check backup codes (always check to prevent timing attacks)
        if not verified:
            with transaction.atomic():
                static_device = StaticDevice.objects.filter(user=user).first()
                if static_device and static_device.verify_token(code):
                    verified = True

        return verified


def complete_first_party_oauth_flow(strategy: DjangoStrategy, user, *args, **kwargs):
    """
    Social auth pipeline stage: redirect to Twig with OAuth code after social login.

    This is called after a user successfully authenticates via Google/GitHub/GitLab
    when they initiated login from a first-party app (like Twig).

    The OAuth params were stored in the session by sso_login() before starting
    the social auth flow.
    """
    oauth_params = strategy.session_get("first_party_oauth_params")
    if not oauth_params:
        return None  # Normal flow, not a first-party app login

    # Validate the application is first-party
    try:
        application = OAuthApplication.objects.get(client_id=oauth_params["client_id"])
    except OAuthApplication.DoesNotExist:
        logger.warning("First-party OAuth flow: unknown client_id", client_id=oauth_params["client_id"])
        return None

    if not application.is_first_party:
        logger.warning(
            "First-party OAuth flow: application is not first-party",
            client_id=oauth_params["client_id"],
        )
        return None

    # Validate redirect_uri
    redirect_uri = oauth_params.get("redirect_uri", "")
    if not redirect_uri:
        logger.warning("First-party OAuth flow: missing redirect_uri")
        return None

    # Validate redirect_uri matches one of the application's registered URIs
    registered_uris = application.redirect_uris.split() if application.redirect_uris else []
    if redirect_uri not in registered_uris:
        logger.warning(
            "First-party OAuth flow: invalid redirect_uri",
            redirect_uri=redirect_uri,
            registered_uris=registered_uris,
        )
        return None

    # Create authorization code
    code = _create_authorization_code(
        user=user,
        application=application,
        redirect_uri=redirect_uri,
        code_challenge=oauth_params.get("code_challenge", ""),
        scope=oauth_params.get("scope", ""),
        state=oauth_params.get("state"),
    )

    # Build redirect URL with code
    final_redirect_uri = f"{redirect_uri}?code={code}"
    if oauth_params.get("state"):
        final_redirect_uri += f"&state={oauth_params['state']}"

    # Clean up session
    strategy.session_delete("first_party_oauth_params")

    logger.info(
        "First-party OAuth flow complete",
        user_id=user.pk,
        client_id=oauth_params["client_id"],
    )

    return redirect(final_redirect_uri)


def _create_authorization_code(
    user: User,
    application: OAuthApplication,
    redirect_uri: str,
    code_challenge: str,
    scope: str,
    state: Optional[str] = None,
) -> str:
    """Create an OAuth authorization code for the user."""
    code_value = secrets.token_urlsafe(32)
    expires = timezone.now() + timedelta(seconds=cast(int, oauth2_settings.AUTHORIZATION_CODE_EXPIRE_SECONDS))

    # Get scoped teams - default to all user's teams
    scoped_teams = list(Team.objects.filter(organization__members=user).values_list("pk", flat=True))

    OAuthGrant.objects.create(
        application=application,
        user=user,
        code=code_value,
        expires=expires,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method="S256" if code_challenge else "",
        nonce="",
        claims="{}",
        scoped_teams=scoped_teams,
        scoped_organizations=[],
    )

    return code_value
