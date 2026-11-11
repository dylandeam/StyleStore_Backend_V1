"""Public, tenant-admin and platform-admin subscription plan endpoints."""
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_optional_current_user
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.database import get_db
from app.config import settings
from app.models.empresa import Empresa
from app.models.plan_suscripcion import ContratacionPlan
from app.models.user import User
from app.schemas.plan_suscripcion import (
    ContratacionCreate,
    ContratacionPayPalCapture,
    ContratacionPayPalCreate,
    ContratacionResponse,
    PlanCreate,
    PlanResponse,
    PlanUpdate,
)
from app.services.plan_service import PlanService
from app.services.paypal_service import paypal_service


router = APIRouter(prefix="/planes", tags=["Planes de Suscripción"])


def _public_empresa_id(db: Session, current_user: User | None, empresa_codigo: str | None) -> int:
    if current_user and current_user.empresa_id is not None:
        return current_user.empresa_id
    code = (empresa_codigo or "stylestore").strip().lower()
    empresa = db.query(Empresa).filter(
        Empresa.codigo == code, Empresa.active.is_(True)
    ).first()
    if not empresa:
        raise NotFoundException("Empresa no encontrada.")
    return empresa.id


@router.get("", response_model=list[PlanResponse], summary="Ver planes disponibles para clientes")
async def list_public_plans(
    empresa_codigo: str | None = Query(None),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    empresa_id = _public_empresa_id(db, current_user, empresa_codigo)
    return PlanService(db).list_plans(empresa_id, active_only=True)


@router.get("/admin", response_model=list[PlanResponse], summary="Administrar planes de suscripción")
async def list_admin_plans(
    empresa_id: int | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = PlanService(db)
    scope_empresa_id = service.resolve_admin_scope(current_user, empresa_id)
    return service.list_plans(scope_empresa_id)


@router.post("/admin", response_model=PlanResponse, status_code=201, summary="Crear un plan global")
async def create_admin_plan(
    payload: PlanCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PlanService(db).create_global_plan(payload, current_user)


@router.get("/admin/suscripciones", summary="Resumen de suscripciones por tenant")
async def list_admin_subscriptions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PlanService(db).subscription_overview(current_user)


@router.put("/admin/{codigo}", response_model=PlanResponse, summary="Configurar un plan")
async def update_admin_plan(
    codigo: str,
    payload: PlanUpdate,
    empresa_id: int | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PlanService(db).update_plan(codigo, payload, current_user, empresa_id)


@router.delete(
    "/admin/{codigo}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un plan global personalizado",
)
async def delete_admin_plan(
    codigo: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    PlanService(db).delete_global_plan(codigo, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/contratar", response_model=ContratacionResponse, summary="Contratar o cambiar plan")
async def contract_plan(
    payload: ContratacionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "administrador":
        raise ForbiddenException("Solo el administrador del tenant puede cambiar el plan.")
    raise BadRequestException(
        "Debes confirmar el pago mediante PayPal; el plan solo se activa después del cobro."
    )


@router.post("/paypal/crear-orden", summary="Iniciar pago PayPal de una suscripción")
async def create_subscription_paypal_order(
    payload: ContratacionPayPalCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = PlanService(db)
    if paypal_service.is_mock_mode() and not settings.ALLOW_MOCK_PAYPAL:
        raise BadRequestException(
            "PayPal no está configurado. Define las credenciales PayPal del entorno antes de cobrar una suscripción."
        )
    pending = service.prepare_paypal_contract(current_user, payload.plan_codigo)
    try:
        order = await paypal_service.create_order(
            orden_id=pending.id,
            total=pending.precio_snapshot,
            return_url=payload.return_url or "",
            cancel_url=payload.cancel_url or "",
            currency=pending.moneda_snapshot,
            reference_prefix="PLAN",
            description=f"Suscripción StyleStore - {pending.plan.nombre}",
            allow_fallback=settings.ALLOW_MOCK_PAYPAL,
        )
        pending.referencia_pago = order.get("id")
        db.commit()
    except Exception as exc:
        db.rollback()
        raise BadRequestException(f"No se pudo iniciar el cobro con PayPal: {exc}") from exc
    order["contratacion_id"] = pending.id
    order["plan_codigo"] = pending.plan.codigo
    order["total"] = float(pending.precio_snapshot)
    order["moneda"] = pending.moneda_snapshot
    return order


@router.post("/paypal/capturar-orden", response_model=ContratacionResponse, summary="Confirmar pago PayPal y activar plan")
async def capture_subscription_paypal_order(
    payload: ContratacionPayPalCapture,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pending = db.query(ContratacionPlan).filter(
        ContratacionPlan.referencia_pago == payload.paypal_order_id,
        ContratacionPlan.empresa_id == current_user.empresa_id,
    ).order_by(ContratacionPlan.id.desc()).first()
    if not pending:
        raise NotFoundException("No existe una suscripción pendiente asociada a esa orden PayPal.")
    if pending.estado_pago == "pagado":
        return PlanService.serialize_contract(pending)
    capture = await paypal_service.capture_order(
        payload.paypal_order_id,
        allow_mock=settings.ALLOW_MOCK_PAYPAL,
    )
    if capture.get("status") != "COMPLETED":
        raise BadRequestException("PayPal no confirmó el cobro; el plan no fue modificado.")
    capture_data = (((capture.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}])[0]
    amount = capture_data.get("amount") or {}
    expected_value, expected_currency = paypal_service.payment_amount(
        pending.precio_snapshot, pending.moneda_snapshot
    )
    if amount and (
        amount.get("currency_code") != expected_currency
        or str(amount.get("value")) != f"{expected_value:.2f}"
    ):
        raise BadRequestException("El importe capturado por PayPal no coincide con el precio del plan.")
    contract = PlanService(db).activate_paid_contract(
        current_user, pending, capture_data.get("id") or payload.paypal_order_id
    )
    return PlanService.serialize_contract(contract)


@router.get("/mi-contratacion", response_model=ContratacionResponse | None, summary="Ver plan contratado")
async def get_my_contract(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.empresa_id is not None:
        PlanService(db).reconcile_tenant_access(current_user.empresa_id)
    contract = db.query(ContratacionPlan).filter(
        ContratacionPlan.empresa_id == current_user.empresa_id,
        ContratacionPlan.estado.in_(["activa", "vencida", "reemplazada"]),
    ).order_by(
        (ContratacionPlan.estado == "activa").desc(), ContratacionPlan.id.desc()
    ).first()
    return PlanService.serialize_contract(contract) if contract else None
