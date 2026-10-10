from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import User


class AgroJWTAuthentication(JWTAuthentication):
    """
    Custom JWT authentication that resolves tokens against our custom User model.
    """

    def get_user(self, validated_token):
        user_id = validated_token.get("user_id")
        if user_id is None:
            raise exceptions.AuthenticationFailed(
                "Token missing user identifier.",
                code="token_no_user_id",
            )

        try:
            user = User.objects.select_related("role").get(id=user_id)
        except User.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed(
                "User not found.",
                code="user_not_found",
            ) from exc

        if user.status == user.STATUS_INACTIVE:
            raise exceptions.AuthenticationFailed(
                "User account is inactive.",
                code="user_inactive",
            )

        return user

