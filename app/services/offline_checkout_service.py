"""Enlaces firmados para iniciar una compra PayPal desde otro dispositivo.

El PWA guarda estos enlaces junto al detalle cacheado de la prenda. El token no
contiene credenciales ni autoriza otras operaciones: únicamente identifica al
cliente, tenant, existencia y cantidad preparados mientras había conexión.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from jose import JWTError, jwt

from app.config import settings
from app.core.exceptions import BadRequestException
from app.models.user import User


class OfflineCheckoutService:
    TOKEN_TYPE = "offline_paypal_checkout"
    TOKEN_DAYS = 30

    @classmethod
    def create_token(cls, user: User, stock_id: int, quantity: int = 1) -> str:
        if user.empresa_id is None or user.role != "cliente" or user.is_platform_admin:
            raise BadRequestException("Solo un cliente del tenant puede preparar una compra offline.")
        if quantity < 1 or quantity > 5:
            raise BadRequestException("La compra offline admite entre 1 y 5 unidades por enlace.")
        now = datetime.now(timezone.utc)
        payload = {
            "type": cls.TOKEN_TYPE,
            "sub": str(user.id),
            "empresa_id": user.empresa_id,
            "stock_id": stock_id,
            "quantity": quantity,
            "iat": now,
            "exp": now + timedelta(days=cls.TOKEN_DAYS),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    @classmethod
    def decode_token(cls, token: str) -> dict[str, int]:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            if payload.get("type") != cls.TOKEN_TYPE:
                raise BadRequestException("El enlace no corresponde a una compra offline.")
            decoded = {
                "user_id": int(payload["sub"]),
                "empresa_id": int(payload["empresa_id"]),
                "stock_id": int(payload["stock_id"]),
                "quantity": int(payload.get("quantity", 1)),
            }
        except (JWTError, KeyError, TypeError, ValueError) as exc:
            raise BadRequestException("El enlace de compra es inválido o venció. Abre la prenda con conexión para renovarlo.") from exc
        if decoded["quantity"] < 1 or decoded["quantity"] > 5:
            raise BadRequestException("La cantidad preparada en el enlace no es válida.")
        return decoded

    @staticmethod
    def operation_uuid(token: str) -> str:
        """UUID estable para que abrir dos veces el mismo enlace no duplique ventas."""
        return str(uuid5(NAMESPACE_URL, f"stylestore-offline-paypal:{token}"))

    @staticmethod
    def public_url(token: str) -> str:
        return f"{settings.FRONTEND_URL.rstrip('/')}/pago-offline/{token}"

    @staticmethod
    def approval_url(paypal_order_id: str) -> str:
        host = "www.paypal.com" if settings.PAYPAL_MODE.lower() == "live" else "www.sandbox.paypal.com"
        return f"https://{host}/checkoutnow?token={paypal_order_id}"
