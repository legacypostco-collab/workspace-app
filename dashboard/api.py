from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from marketplace.models import UserProfile

from .models import DashboardProjection
from .services import DashboardProjectionBuilder


class SupplierDashboardAPIView(APIView):
    permission_classes = [IsAuthenticated]
    stale_after = timedelta(minutes=5)

    def get(self, request):
        profile = UserProfile.objects.filter(user=request.user).first()
        if not profile or profile.role != "seller":
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)

        projection = DashboardProjection.objects.filter(supplier=request.user, user=request.user).first()
        is_stale = True
        if projection and projection.updated_at:
            is_stale = projection.updated_at < (timezone.now() - self.stale_after)
        if projection is None or is_stale:
            projection = DashboardProjectionBuilder().build(supplier=request.user, user=request.user)

        payload = DashboardProjectionBuilder().payload(projection)
        return Response(payload, status=status.HTTP_200_OK)
