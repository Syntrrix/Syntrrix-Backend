from typing import Optional

from rest_framework.permissions import BasePermission

from .models import User


class _BaseRolePermission(BasePermission):
    required_role_id: Optional[int] = None

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return False
        if self.required_role_id is None:
            return False
        return user.role_id == self.required_role_id


class IsAdmin(_BaseRolePermission):
    required_role_id = 1


class IsBuyer(_BaseRolePermission):
    required_role_id = 2


class IsSeller(_BaseRolePermission):
    required_role_id = 3


class IsSellerOrTeamManagerOrAgent(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return False
        # Role IDs: 3=Seller, 4=Team Manager, 5=Agent
        return user.role_id in (3, 4, 5)


