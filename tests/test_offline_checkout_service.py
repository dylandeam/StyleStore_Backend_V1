from jose import jwt

from app.config import settings
from app.core.exceptions import BadRequestException
from app.models.user import User
from app.services.offline_checkout_service import OfflineCheckoutService


def _client_user() -> User:
    return User(id=42, empresa_id=7, email="cliente@example.com", name="Ana", role="cliente")


def test_offline_checkout_token_is_bound_to_user_tenant_stock_and_quantity():
    token = OfflineCheckoutService.create_token(_client_user(), stock_id=19, quantity=3)

    assert OfflineCheckoutService.decode_token(token) == {
        "user_id": 42,
        "empresa_id": 7,
        "stock_id": 19,
        "quantity": 3,
    }
    assert OfflineCheckoutService.operation_uuid(token) == OfflineCheckoutService.operation_uuid(token)
    assert "/pago-offline/" in OfflineCheckoutService.public_url(token)


def test_offline_checkout_rejects_a_token_for_another_purpose():
    token = jwt.encode(
        {"type": "access", "sub": "42", "empresa_id": 7, "stock_id": 19, "quantity": 1},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )

    try:
        OfflineCheckoutService.decode_token(token)
        assert False, "El token de acceso no debe aceptarse como enlace de compra"
    except BadRequestException as exc:
        assert "no corresponde" in str(exc).lower()
