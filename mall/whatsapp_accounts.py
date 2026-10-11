"""WhatsApp de la cuenta: an account's own WhatsApp number (AccountWhatsApp), next to its people's. Any member sees it; its owner or an
administrator sets it, verifies it with the code sent there (mall.whatsapp_verification) and chooses its notices. It gets the account's
notices, and what it writes back goes into the deal's chat on the account's behalf (mall.whatsapp_bridge)."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .inbox_models import AccountWhatsApp
from .models import Membership
from .whatsapp import WhatsAppPreferences, change, preferences
from .whatsapp_verification import CodeSerializer, confirm

MANAGERS = ('owner', 'manager')


def holder_for(request, account_id, manage):
    membership = Membership.objects.filter(user=request.user, account_id=account_id, account__active=True).select_related('account').first()
    if membership is None:
        raise NotFound('No perteneces a esta cuenta.')
    if manage and membership.permission not in MANAGERS:
        raise PermissionDenied('Solo el propietario o un administrador de la cuenta configura su WhatsApp.')
    return AccountWhatsApp.objects.get_or_create(account=membership.account)[0], membership.permission in MANAGERS


class AccountWhatsAppView(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='El WhatsApp propio de la cuenta: número, si está verificado y activo, y qué '
                   'avisos recibe; can_manage dice si la persona puede cambiarlo (propietario o administrador).')
    def get(self, request, account_id):
        holder, can_manage = holder_for(request, account_id, manage=False)
        return Response({**preferences(holder), 'can_manage': can_manage})

    @extend_schema(request=WhatsAppPreferences, responses=OpenApiTypes.OBJECT, description='Como inbox/whatsapp/, para el número de la cuenta: '
                   'un número nuevo recibe un código por WhatsApp y queda pendiente hasta confirmarlo en whatsapp/verify/.')
    def post(self, request, account_id):
        holder, _ = holder_for(request, account_id, manage=True)
        response = change(request, holder)
        if response.status_code == 200:
            response.data = {**response.data, 'can_manage': True}
        return response


class AccountWhatsAppVerify(APIView):
    @extend_schema(request=CodeSerializer, responses=OpenApiTypes.OBJECT, description='Confirma el número pendiente de la cuenta con el código '
                   'que se le envió; queda verificado a nombre de quien lo confirma, que firma lo que la cuenta responda por WhatsApp.')
    def post(self, request, account_id):
        holder, _ = holder_for(request, account_id, manage=True)
        data = CodeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        holder = confirm(request.user, AccountWhatsApp, holder.pk, data.validated_data['code'].strip(), whatsapp_verified_by=request.user)
        return Response({**preferences(holder), 'can_manage': True})
