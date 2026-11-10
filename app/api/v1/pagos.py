"""
Pagos endpoints (v5 sección 19 y 22, Guía de Pagos & Caja POS).
Soporta:
- PayPal Orders v2 (crear orden y capturar fondos con idempotencia).
- Caja / Cobro presencial en efectivo con cálculo de vuelto y tickets de venta.
- Consulta de recibos / historial.
"""
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings
from app.models.user import User
from app.models.cliente import Cliente
from app.models.orden_venta import OrdenVenta, DetalleVenta
from app.models.reserva import Reserva
from app.models.pago import Pago
from app.models.stock_inventario import StockInventario
from app.models.qr_pago_config import QRPagoConfig
from app.models.empresa import Empresa
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.schemas.pago_envio import (
    PagoCreateRequest,
    PagoResponse,
    PayPalCrearOrdenRequest,
    PayPalCapturarOrdenRequest,
    PayPalReconciliationItem,
    CobroCajaRequest,
    CobroCajaResponse,
    QRConfigDTO,
    QRConfigResponse,
    OfflineCheckoutPreview,
    OfflineCheckoutStartResponse,
)
from app.api.deps import get_current_user, get_optional_current_user, require_permission, require_plan_capability
from app.core.exceptions import NotFoundException, BadRequestException
from app.services.bitacora_service import BitacoraService
from app.services.paypal_service import paypal_service
from app.services.tenant_scope import effective_empresa_id, scope_by_sucursal
from app.services.pago_service import PagoService
from app.services.payment_reconciliation_service import PaymentReconciliationService
from app.services.offline_checkout_service import OfflineCheckoutService
from app.services.promocion_service import PromocionService
from app.services.plan_service import PlanService
from app.services.notificacion_service import NotificacionService

router = APIRouter(prefix="/pagos", tags=["Pagos"])


def _mark_reservation_paid(db: Session, orden: OrdenVenta) -> None:
    reserva = db.query(Reserva).filter(Reserva.orden_venta_id == orden.id).first()
    if reserva and reserva.estado == "pendiente_pago":
        reserva.estado = "pagada_pendiente_entrega"


def _offline_checkout_context(token: str, db: Session) -> dict:
    """Resolve and revalidate every boundary encoded in a public checkout link."""
    claims = OfflineCheckoutService.decode_token(token)
    user = db.query(User).filter(
        User.id == claims["user_id"],
        User.empresa_id == claims["empresa_id"],
        User.role == "cliente",
        User.is_active.is_(True),
    ).first()
    if not user:
        raise BadRequestException("La cuenta asociada a este enlace ya no está disponible.")

    empresa = db.query(Empresa).filter(Empresa.id == claims["empresa_id"]).first()
    if not empresa or not empresa.active:
        raise BadRequestException("La tienda asociada a este enlace no está disponible temporalmente.")

    PlanService(db).require_capability(user, "ventas")
    has_permission = (
        db.query(RolePermission)
        .join(Permission, RolePermission.permission_id == Permission.id)
        .join(Role, RolePermission.role_id == Role.id)
        .filter(
            Permission.code == "ventas.comprar",
            RolePermission.role_id == user.role_id,
            Role.active.is_(True),
            Role.empresa_id == user.empresa_id,
        )
        .first()
    )
    if not has_permission:
        raise BadRequestException("Las compras no están disponibles temporalmente en esta tienda.")

    cliente = db.query(Cliente).filter(Cliente.user_id == user.id).first()
    stock = db.query(StockInventario).filter(StockInventario.id == claims["stock_id"]).first()
    if not cliente or not stock or not stock.sucursal or not stock.producto_color:
        raise BadRequestException("La prenda preparada ya no está disponible.")
    producto = stock.producto_color.producto
    if stock.sucursal.empresa_id != claims["empresa_id"] or producto.empresa_id != claims["empresa_id"]:
        raise BadRequestException("El enlace no pertenece a esta tienda.")

    _, precio, _ = PromocionService(db).precio_stock(stock)
    cantidad = claims["quantity"]
    total = Decimal(precio * cantidad).quantize(Decimal("0.01"))
    operation_uuid = OfflineCheckoutService.operation_uuid(token)
    pago = db.query(Pago).filter(Pago.operation_uuid == operation_uuid).first()
    orden = pago.orden_venta if pago else None
    estado = pago.estado if pago else ("agotado" if stock.cantidad < cantidad else "preparado")
    return {
        "user": user,
        "cliente": cliente,
        "stock": stock,
        "producto": producto,
        "cantidad": cantidad,
        "precio": precio,
        "total": total,
        "operation_uuid": operation_uuid,
        "pago": pago,
        "orden": orden,
        "estado": estado,
    }


def _offline_checkout_payload(context: dict) -> dict:
    stock = context["stock"]
    user = context["user"]
    return {
        "producto": context["producto"].nombre,
        "color": getattr(stock.producto_color.color, "nombre", None),
        "talla": getattr(stock.talla, "nombre", None),
        "sucursal": stock.sucursal.name,
        "cantidad": context["cantidad"],
        "precio_unitario": context["precio"],
        "total": context["total"],
        "moneda": "BOB",
        "cliente": f"{user.name} {user.apellido or ''}".strip(),
        "estado": context["estado"],
        "orden_venta_id": context["orden"].id if context["orden"] else None,
    }


def _payment_company_id(order: OrdenVenta, current_user: User | None = None) -> int:
    empresa_id = order.sucursal.empresa_id if order.sucursal else None
    if empresa_id is None and current_user:
        empresa_id = current_user.empresa_id
    if empresa_id is None and order.cliente and order.cliente.user:
        empresa_id = order.cliente.user.empresa_id
    if empresa_id is None:
        raise BadRequestException("No se pudo identificar la empresa asociada al pago.")
    return empresa_id


def _scoped_order(db: Session, current_user: User, order_id: int) -> OrdenVenta | None:
    query = db.query(OrdenVenta).filter(OrdenVenta.id == order_id)
    order = scope_by_sucursal(query, db, current_user, OrdenVenta.sucursal_id).first()
    if order and current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente or order.codigo_cliente != cliente.codigo:
            return None
    return order


def _scoped_payment(db: Session, current_user: User, payment_id: int) -> Pago | None:
    query = db.query(Pago).join(OrdenVenta, Pago.orden_venta_id == OrdenVenta.id).filter(Pago.id == payment_id)
    empresa_id = effective_empresa_id(current_user)
    if empresa_id is not None:
        query = query.filter(Pago.empresa_id == empresa_id)
    query = scope_by_sucursal(query, db, current_user, OrdenVenta.sucursal_id)
    payment = query.first()
    if payment and current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente or payment.orden_venta.codigo_cliente != cliente.codigo:
            return None
    return payment


# ==========================================
# 1. PAYPAL ORDERS V2
# ==========================================

@router.get("/paypal/estado", summary="Comprobar configuración segura de PayPal")
async def get_paypal_status(
    current_user: User = Depends(get_current_user),
):
    """Expose readiness and mode without returning credentials or secret values."""
    configured = not paypal_service.is_mock_mode()
    return {
        "configured": configured,
        "mode": paypal_service.mode,
        "sandbox": paypal_service.mode == "sandbox",
        "mock_enabled": settings.ALLOW_MOCK_PAYPAL,
        "ready_for_sandbox": configured and paypal_service.mode == "sandbox",
        "bob_per_usd": settings.PAYPAL_BOB_PER_USD,
    }


@router.get(
    "/paypal/offline/{token}",
    response_model=OfflineCheckoutPreview,
    summary="Consultar una compra offline preparada",
)
async def get_offline_checkout(token: str, db: Session = Depends(get_db)):
    """Public preview safe to open on a second device; it exposes no credentials."""
    return _offline_checkout_payload(_offline_checkout_context(token, db))


@router.post(
    "/paypal/offline/{token}/crear-orden",
    response_model=OfflineCheckoutStartResponse,
    summary="Iniciar PayPal para una compra offline preparada",
)
async def create_offline_paypal_order(token: str, db: Session = Depends(get_db)):
    """Create an idempotent PayPal order owned by the client encoded in the signed link."""
    context = _offline_checkout_context(token, db)
    existing_payment = context["pago"]
    if existing_payment:
        payload = _offline_checkout_payload(context)
        if not existing_payment.paypal_order_id:
            raise BadRequestException("El cobro preparado no tiene una orden PayPal válida.")
        return {
            **payload,
            "paypal_order_id": existing_payment.paypal_order_id,
            "approve_url": OfflineCheckoutService.approval_url(existing_payment.paypal_order_id),
            "paypal_total": None,
            "paypal_currency": None,
        }

    stock = context["stock"]
    if stock.cantidad < context["cantidad"]:
        raise BadRequestException(
            "La prenda se agotó antes de iniciar el pago. No se realizó ningún cobro."
        )

    orden = OrdenVenta(
        client_operation_uuid=context["operation_uuid"],
        fecha=datetime.now().date(),
        estado="pendiente_pago",
        total=context["total"],
        subtotal_productos=context["total"],
        tipo_venta="en linea",
        codigo_cliente=context["cliente"].codigo,
        comprador_nombre=context["cliente"].nombre,
        sucursal_id=stock.sucursal_id,
        metodo_pago="PAYPAL",
        inventario_descontado=False,
        tipo_entrega="recojo",
    )
    detalle = DetalleVenta(
        orden_venta=orden,
        stock_inventario_id=stock.id,
        producto_nombre=context["producto"].nombre,
        color_nombre=getattr(stock.producto_color.color, "nombre", None),
        talla_nombre=getattr(stock.talla, "nombre", None),
        cantidad=context["cantidad"],
        precio_unitario=context["precio"],
        subtotal=context["total"],
    )
    db.add_all([orden, detalle])
    try:
        db.flush()
        order_data = await paypal_service.create_order(
            orden_id=orden.id,
            total=orden.total,
            return_url=f"{settings.FRONTEND_URL.rstrip('/')}/paypal-return?offline_checkout=1",
            cancel_url=f"{OfflineCheckoutService.public_url(token)}?cancelado=1",
            currency="BOB",
            reference_prefix="OFFLINE",
            description=f"{context['producto'].nombre} · compra offline #{orden.id}",
            allow_fallback=settings.ALLOW_MOCK_PAYPAL,
        )
        paypal_order_id = order_data.get("id")
        if not paypal_order_id:
            raise RuntimeError("PayPal no devolvió un identificador de orden.")
        payment = Pago(
            orden_venta_id=orden.id,
            empresa_id=context["user"].empresa_id,
            monto=orden.total,
            tipo_pago="en linea",
            metodo_pago="paypal",
            estado="pendiente",
            paypal_order_id=paypal_order_id,
            operation_uuid=context["operation_uuid"],
        )
        db.add(payment)
        db.commit()
        db.refresh(orden)
    except Exception as exc:
        db.rollback()
        raise BadRequestException(f"No se pudo preparar el cobro con PayPal: {exc}") from exc

    NotificacionService(db).notificar_nueva_venta_online(orden)
    BitacoraService.registrar(
        db,
        context["user"],
        f"Preparó compra offline vinculada a la orden #{orden.id}",
        "Pagos PayPal",
    )
    context.update({"pago": payment, "orden": orden, "estado": "pendiente"})
    approve_url = next(
        (link.get("href") for link in order_data.get("links", []) if link.get("rel") == "approve"),
        OfflineCheckoutService.approval_url(paypal_order_id),
    )
    return {
        **_offline_checkout_payload(context),
        "paypal_order_id": paypal_order_id,
        "approve_url": approve_url,
        "paypal_total": order_data.get("paypal_total"),
        "paypal_currency": order_data.get("paypal_currency"),
    }

@router.post("/paypal/crear-orden", summary="Crear orden en PayPal Orders v2", dependencies=[Depends(require_plan_capability("ventas")), Depends(require_permission("ventas.comprar"))])
async def create_paypal_order(
    payload: PayPalCrearOrdenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Crea una orden en PayPal v2 para el checkout de StyleStore.
    """
    orden = _scoped_order(db, current_user, payload.orden_venta_id)
    if not orden:
        raise NotFoundException(f"Orden de venta con ID {payload.orden_venta_id} no encontrada.")

    if orden.estado == "pagada":
        raise BadRequestException("Esta orden ya ha sido pagada previamente.")

    if paypal_service.is_mock_mode() and not settings.ALLOW_MOCK_PAYPAL:
        raise BadRequestException(
            "PayPal no está configurado. Define credenciales válidas antes de iniciar el cobro."
        )

    try:
        order_data = await paypal_service.create_order(
            orden_id=orden.id,
            total=orden.total,
            return_url=payload.return_url or "",
            cancel_url=payload.cancel_url or "",
            currency="BOB",
            allow_fallback=settings.ALLOW_MOCK_PAYPAL,
        )
    except Exception as exc:
        raise BadRequestException(f"No se pudo iniciar el cobro con PayPal: {exc}") from exc

    paypal_order_id = order_data.get("id")

    # Crear o actualizar registro provisional de pago
    pago_existente = db.query(Pago).filter(Pago.orden_venta_id == orden.id).first()
    if not pago_existente:
        pago_existente = Pago(
            orden_venta_id=orden.id,
            empresa_id=_payment_company_id(orden, current_user),
            monto=orden.total,
            tipo_pago="en linea",
            estado="pendiente",
            paypal_order_id=paypal_order_id,
            operation_uuid=str(uuid4()),
        )
        db.add(pago_existente)
    else:
        pago_existente.paypal_order_id = paypal_order_id
        pago_existente.tipo_pago = "en linea"
        pago_existente.estado = "pendiente"
        pago_existente.operation_uuid = pago_existente.operation_uuid or str(uuid4())

    db.commit()

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Inició checkout con PayPal para Orden #{orden.id} (PayPal ID: {paypal_order_id})",
        module="pagos",
    )

    order_data["orden_id"] = orden.id
    order_data["orden_total_bob"] = float(orden.total)
    order_data["total"] = float(orden.total)

    return order_data


@router.post("/paypal/capturar-orden", response_model=PagoResponse, summary="Capturar fondos de PayPal")
async def capture_paypal_order(
    payload: PayPalCapturarOrdenRequest,
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Captura los fondos tras la aprobación del cliente en PayPal.
    Implementa idempotencia para evitar errores si la orden ya fue procesada,
    y resuelve la orden automáticamente si se proporciona solo el token de PayPal.
    """
    pago = db.query(Pago).filter(Pago.paypal_order_id == payload.paypal_order_id).first()
    if not pago:
        raise NotFoundException("No se encontró una orden asociada al identificador PayPal.")
    if current_user and not _scoped_order(db, current_user, pago.orden_venta_id):
        raise NotFoundException("La orden PayPal no pertenece al tenant o usuario activo.")

    reconciled, message = await PaymentReconciliationService(db).reconcile(pago)
    if reconciled.estado not in {"aprobado", "reembolsado"}:
        raise BadRequestException(message)
    return reconciled


@router.post(
    "/paypal/reconciliar-pendientes",
    response_model=list[PayPalReconciliationItem],
    summary="Conciliar cobros aprobados desde otro dispositivo",
    dependencies=[Depends(require_plan_capability("ventas"))],
)
async def reconcile_pending_paypal_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reconoce aprobaciones externas sin depender del navegador que abrió el enlace."""
    query = db.query(Pago).join(OrdenVenta, Pago.orden_venta_id == OrdenVenta.id).filter(
        Pago.estado.in_(["pendiente", "reembolso_pendiente"]),
        Pago.paypal_order_id.isnot(None),
    )
    empresa_id = effective_empresa_id(current_user)
    if empresa_id is not None:
        query = query.filter(Pago.empresa_id == empresa_id)
    if current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente:
            return []
        query = query.filter(OrdenVenta.codigo_cliente == cliente.codigo)
    else:
        query = scope_by_sucursal(query, db, current_user, OrdenVenta.sucursal_id)

    results: list[PayPalReconciliationItem] = []
    for candidate in query.order_by(Pago.id.asc()).limit(25).all():
        payment, message = await PaymentReconciliationService(db).reconcile(candidate)
        results.append(PayPalReconciliationItem(
            orden_venta_id=payment.orden_venta_id,
            payment_operation_uuid=payment.operation_uuid or "",
            estado=payment.estado,
            paypal_order_id=payment.paypal_order_id or "",
            paypal_capture_id=payment.paypal_capture_id,
            paypal_refund_id=payment.paypal_refund_id,
            mensaje=message,
        ))
    return results


# ==========================================
# 2. COBRO EN CAJA (POS / EFECTIVO)
# ==========================================

@router.post("/caja", response_model=CobroCajaResponse, summary="Procesar cobro en caja con cálculo de vuelto", dependencies=[Depends(require_plan_capability("ventas"))])
async def process_caja_payment(
    payload: CobroCajaRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Registra cobro en efectivo presencial en el POS / Punto de Venta.
    Calcula vuelto y genera ticket numerado de venta.
    """
    orden = _scoped_order(db, current_user, payload.orden_venta_id)
    if not orden:
        raise NotFoundException(f"Orden de venta con ID {payload.orden_venta_id} no encontrada.")

    if orden.estado == "pagada":
        raise BadRequestException("Esta orden ya ha sido cobrada y está pagada.")

    metodo = (payload.metodo_pago or "efectivo").lower()
    if metodo == "qr":
        monto_recibido = orden.total
        cambio = Decimal("0.00")
    else:
        if payload.efectivo_recibido < orden.total:
            diff = orden.total - payload.efectivo_recibido
            raise BadRequestException(f"El monto recibido (Bs. {payload.efectivo_recibido}) es inferior al total a pagar (Bs. {orden.total}). Faltan: Bs. {diff}")
        monto_recibido = payload.efectivo_recibido
        cambio = payload.efectivo_recibido - orden.total

    ticket_num = f"TKT-{datetime.now().strftime('%Y%m%d')}-{orden.id:04d}"

    orden.estado = "pagada"
    orden.metodo_pago = metodo
    orden.ticket_numero = ticket_num
    orden.efectivo_recibido = monto_recibido
    orden.cambio_devuelto = cambio
    _mark_reservation_paid(db, orden)

    pago = db.query(Pago).filter(Pago.orden_venta_id == orden.id).first()
    if not pago:
        pago = Pago(
            orden_venta_id=orden.id,
            empresa_id=_payment_company_id(orden, current_user),
            monto=orden.total,
            tipo_pago="qr" if metodo == "qr" else "en caja",
            metodo_pago=metodo,
            estado="aprobado",
            operation_uuid=str(uuid4()),
        )
        db.add(pago)
    else:
        pago.tipo_pago = "qr" if metodo == "qr" else "en caja"
        pago.metodo_pago = metodo
        pago.estado = "aprobado"
        pago.operation_uuid = pago.operation_uuid or str(uuid4())

    db.commit()
    db.refresh(pago)
    PagoService(db).confirmar(pago)

    desc_pago = f"[{metodo.upper()}]" if metodo == "qr" else f"Recibido: Bs. {monto_recibido}, Cambio: Bs. {cambio}"
    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Emitió ticket de caja {ticket_num} para Orden #{orden.id}. {desc_pago}",
        module="pagos",
    )

    return CobroCajaResponse(
        pago_id=pago.id,
        orden_venta_id=orden.id,
        total=orden.total,
        efectivo_recibido=monto_recibido,
        cambio_devuelto=cambio,
        ticket_numero=ticket_num,
        recibo_numero=pago.recibo_numero,
        fecha=datetime.now(),
    )


# ==========================================
# 3. PROCESAR PAGO GENÉRICO / HISTORIAL
# ==========================================

@router.post("", response_model=PagoResponse, status_code=status.HTTP_201_CREATED, summary="Procesar pago general", dependencies=[Depends(require_plan_capability("ventas"))])
async def process_payment(
    payload: PagoCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Registra el pago general de una orden de venta.
    """
    orden = _scoped_order(db, current_user, payload.orden_venta_id)
    if not orden:
        raise NotFoundException(f"Orden de venta con ID {payload.orden_venta_id} no encontrada.")

    pp_id = payload.paypal_order_id or f"PAYPAL-SIM-{orden.id}"

    pago = Pago(
        orden_venta_id=orden.id,
        empresa_id=_payment_company_id(orden, current_user),
        monto=orden.total,
        tipo_pago=payload.tipo_pago,
        estado="aprobado",
        paypal_order_id=pp_id,
        paypal_capture_id=payload.paypal_capture_id,
        operation_uuid=str(uuid4()),
    )
    db.add(pago)

    orden.estado = "pagada"
    orden.metodo_pago = payload.tipo_pago
    _mark_reservation_paid(db, orden)
    db.commit()
    db.refresh(pago)
    PagoService(db).confirmar(pago)

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Registró pago #{pago.id} ({pago.tipo_pago}) por ${pago.monto} para Orden #{orden.id}",
        module="pagos",
    )

    return pago


@router.get("/mios", response_model=list[PagoResponse], summary="Historial de pagos del cliente")
async def list_my_payments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene el historial de pagos del cliente en sesión."""
    cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
    if not cliente:
        return []

    pagos = (
        db.query(Pago)
        .join(OrdenVenta, Pago.orden_venta_id == OrdenVenta.id)
        .filter(
            OrdenVenta.codigo_cliente == cliente.codigo,
            Pago.empresa_id == current_user.empresa_id,
        )
        .order_by(Pago.created_at.desc())
        .all()
    )
    return pagos


# ==========================================
# 4. CONFIGURACIÓN DE QR DE PAGO EN MOSTRADOR
# ==========================================

@router.get("/config-qr", response_model=QRConfigResponse, summary="Obtener configuración del QR de cobro")
async def get_qr_config(
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """Retorna la imagen y datos del QR de cobro vigente para mostrador y pagos."""
    empresa_id = current_user.empresa_id if current_user else None
    if empresa_id is None:
        default_empresa = db.query(Empresa).filter(Empresa.codigo == "stylestore").first()
        empresa_id = default_empresa.id if default_empresa else None
    config = db.query(QRPagoConfig).filter(
        QRPagoConfig.activo == True, QRPagoConfig.empresa_id == empresa_id
    ).order_by(QRPagoConfig.updated_at.desc()).first()
    if not config:
        return QRConfigResponse(
            id=None,
            imagen_url=None,
            banco_destino="QR Simple BNB / BCP / Banco Unión",
            titular="StyleStore Bolivia",
            activo=False,
            updated_at=None,
        )
    return QRConfigResponse(
        id=config.id,
        imagen_url=config.imagen_url,
        banco_destino=config.banco_destino,
        titular=config.titular,
        activo=config.activo,
        updated_at=config.updated_at.isoformat() if config.updated_at else None,
    )


@router.post("/config-qr", response_model=QRConfigResponse, summary="Actualizar QR de cobro en mostrador")
async def update_qr_config(
    payload: QRConfigDTO,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permite al encargado o administrador subir o actualizar la imagen del QR para cobro."""
    if payload.sucursal_id:
        from app.services.tenant_scope import get_scoped_sucursal
        get_scoped_sucursal(db, current_user, payload.sucursal_id)
    config = db.query(QRPagoConfig).filter(
        QRPagoConfig.activo == True,
        QRPagoConfig.empresa_id == current_user.empresa_id,
    ).order_by(QRPagoConfig.updated_at.desc()).first()
    if not config:
        config = QRPagoConfig(
            empresa_id=current_user.empresa_id,
            imagen_url=payload.imagen_url,
            banco_destino=payload.banco_destino or "QR Simple BNB / BCP / Banco Unión",
            titular=payload.titular or "StyleStore Bolivia",
            sucursal_id=payload.sucursal_id,
            activo=True,
        )
        db.add(config)
    else:
        config.imagen_url = payload.imagen_url
        if payload.banco_destino:
            config.banco_destino = payload.banco_destino
        if payload.titular:
            config.titular = payload.titular
        if payload.sucursal_id:
            config.sucursal_id = payload.sucursal_id
        config.activo = True

    db.commit()
    db.refresh(config)

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Actualizó la imagen del código QR de cobro ({config.banco_destino})",
        module="pagos",
    )

    return QRConfigResponse(
        id=config.id,
        imagen_url=config.imagen_url,
        banco_destino=config.banco_destino,
        titular=config.titular,
        activo=config.activo,
        updated_at=config.updated_at.isoformat() if config.updated_at else None,
    )


@router.get("/{pago_id}", summary="Obtener recibo / nota de venta")
async def get_payment_receipt(
    pago_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Genera el detalle completo de recibo / nota de venta."""
    pago = _scoped_payment(db, current_user, pago_id)
    if not pago:
        raise NotFoundException(f"Pago con ID {pago_id} no encontrado.")

    orden = pago.orden_venta
    items = []
    if orden:
        for d in orden.detalles:
            items.append({
                "producto": d.producto_nombre,
                "color": d.color_nombre,
                "talla": d.talla_nombre,
                "cantidad": d.cantidad,
                "precio_unitario": d.precio_unitario,
                "subtotal": d.subtotal,
            })

    # Algunas órdenes históricas de cambios quedaron con total 0 aunque sus
    # líneas sí tenían importe. El recibo debe representar el dinero cobrado.
    total_lineas = sum((Decimal(str(item["subtotal"] or 0)) for item in items), Decimal("0.00"))
    total_recibo = orden.total if orden and orden.total and orden.total > 0 else pago.monto
    if not total_recibo or total_recibo <= 0:
        total_recibo = total_lineas

    return {
        "pago_id": pago.id,
        "recibo_numero": pago.recibo_numero,
        "empresa_id": pago.empresa_id,
        "empresa_nombre": pago.empresa.nombre if pago.empresa else None,
        "orden_venta_id": pago.orden_venta_id,
        "fecha": pago.created_at,
        "monto": pago.monto,
        "tipo_pago": pago.tipo_pago,
        "estado": pago.estado,
        "email_confirmacion_enviado_at": pago.email_confirmacion_enviado_at,
        "paypal_order_id": pago.paypal_order_id,
        "paypal_capture_id": pago.paypal_capture_id,
        "ticket_numero": orden.ticket_numero if orden else None,
        "efectivo_recibido": orden.efectivo_recibido if orden else None,
        "cambio_devuelto": orden.cambio_devuelto if orden else None,
        "cliente_codigo": orden.codigo_cliente if orden else None,
        "comprador_nombre": orden.comprador_nombre if orden else None,
        "comprador_ci": orden.comprador_ci if orden else None,
        "items": items,
        "total": total_recibo,
    }


@router.get("", summary="Listar todos los pagos para Gestionar Pagos (Staff/Admin)")
async def list_all_payments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna el listado completo de pagos con datos de la orden y ticket asociado."""
    pagos_query = db.query(Pago).join(OrdenVenta, Pago.orden_venta_id == OrdenVenta.id)
    pagos = scope_by_sucursal(
        pagos_query, db, current_user, OrdenVenta.sucursal_id
    ).order_by(Pago.created_at.desc()).all()
    resultado = []
    for p in pagos:
        orden = p.orden_venta
        resultado.append({
            "id": p.id,
            "orden_venta_id": p.orden_venta_id,
            "empresa_id": p.empresa_id,
            "empresa_nombre": p.empresa.nombre if p.empresa else None,
            "recibo_numero": p.recibo_numero,
            "monto": float(p.monto) if p.monto else 0.0,
            "tipo_pago": p.tipo_pago,
            "estado": p.estado,
            "paypal_order_id": p.paypal_order_id,
            "ticket_numero": orden.ticket_numero if orden else None,
            "cliente_codigo": orden.codigo_cliente if orden else None,
            "comprador_nombre": orden.comprador_nombre if orden else None,
            "comprador_ci": orden.comprador_ci if orden else None,
            "total_orden": float(orden.total) if orden and orden.total else float(p.monto),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return resultado


@router.post("/confirmar-online/{orden_id}", response_model=CobroCajaResponse, summary="Confirmar cobro de orden online y emitir ticket (sin vuelto)", dependencies=[Depends(require_plan_capability("ventas"))])
async def confirmar_cobro_online(
    orden_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Confirma el cobro de una orden de compra en línea directamente en Gestionar Pagos.
    No requiere cálculo de vuelto; emite ticket inmediatamente y actualiza estado a pagada.
    """
    orden = _scoped_order(db, current_user, orden_id)
    if not orden:
        raise NotFoundException(f"Orden de venta #{orden_id} no encontrada.")

    if orden.estado == "pagada":
        raise BadRequestException(f"La orden #{orden_id} ya se encuentra pagada.")

    ticket_num = f"TKT-ONL-{datetime.now().strftime('%Y%m%d')}-{orden.id:04d}"

    orden.estado = "pagada"
    orden.metodo_pago = orden.metodo_pago or "en linea"
    orden.ticket_numero = ticket_num
    orden.efectivo_recibido = orden.total
    orden.cambio_devuelto = Decimal("0.00")
    _mark_reservation_paid(db, orden)

    pago = db.query(Pago).filter(Pago.orden_venta_id == orden.id).first()
    if not pago:
        pago = Pago(
            orden_venta_id=orden.id,
            empresa_id=_payment_company_id(orden, current_user),
            monto=orden.total,
            tipo_pago="en linea",
            estado="aprobado",
            paypal_order_id=f"ONLINE-CONFIRMED-{orden.id}",
            operation_uuid=str(uuid4()),
        )
        db.add(pago)
    else:
        pago.estado = "aprobado"
        pago.tipo_pago = orden.metodo_pago or "en linea"
        pago.operation_uuid = pago.operation_uuid or str(uuid4())

    db.commit()
    db.refresh(pago)
    PagoService(db).confirmar(pago)

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Confirmó cobro de compra en línea y emitió ticket {ticket_num} para Orden #{orden.id}",
        module="pagos",
    )

    return CobroCajaResponse(
        pago_id=pago.id,
        orden_venta_id=orden.id,
        total=orden.total,
        efectivo_recibido=orden.total,
        cambio_devuelto=Decimal("0.00"),
        ticket_numero=ticket_num,
        recibo_numero=pago.recibo_numero,
        fecha=datetime.now(),
    )


@router.delete("/{pago_id}", status_code=status.HTTP_200_OK, summary="Eliminar o anular pago")
async def delete_payment(
    pago_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Elimina o revierte un registro de pago y bitacora la acción (para administradores)."""
    pago = _scoped_payment(db, current_user, pago_id)
    if not pago:
        raise NotFoundException(f"Pago #{pago_id} no encontrado.")

    orden = pago.orden_venta
    if orden:
        orden.estado = "pendiente_pago"
        orden.ticket_numero = None

    db.delete(pago)
    db.commit()

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Eliminó/anuló el pago #{pago_id} asociado a la orden #{orden.id if orden else 'N/A'}",
        module="pagos",
    )
    return {"message": f"Pago #{pago_id} eliminado exitosamente."}
