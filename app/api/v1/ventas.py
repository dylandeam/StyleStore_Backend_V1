"""
Ventas endpoints (v5 secciones 18, 21 y 23).
Gestión de ventas presenciales y en línea, e historial de compras del cliente.
"""
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app.models.user import User
from app.models.user_empresa import UserEmpresa
from app.models.cliente import Cliente
from app.models.orden_venta import OrdenVenta, DetalleVenta
from app.models.pago import Pago
from app.models.stock_inventario import StockInventario
from app.models.sucursal import Sucursal
from app.schemas.pago_envio import (
    OrdenVentaResponse,
    VentaPresencialCreate,
    DetalleVentaResponse,
)
from app.api.deps import get_current_user, require_permission, require_plan_capability
from app.core.exceptions import NotFoundException, BadRequestException
from app.services.bitacora_service import BitacoraService
from app.services.tenant_scope import get_scoped_sucursal, scope_by_sucursal
from app.services.promocion_service import PromocionService
from app.services.pago_service import PagoService

router = APIRouter(prefix="/ventas", tags=["Ventas"])


def _client_orders_query(db: Session, current_user: User, cliente: Cliente):
    """Aísla también el historial del cliente al tenant activo."""
    return db.query(OrdenVenta).filter(
        OrdenVenta.codigo_cliente == cliente.codigo,
        OrdenVenta.sucursal_id.in_(
            db.query(Sucursal.id).filter(Sucursal.empresa_id == current_user.empresa_id)
        ),
    )


def _serialize_orden(o: OrdenVenta) -> dict:
    pago = max(o.pagos, key=lambda item: item.id, default=None)
    detalles = []
    for d in o.detalles:
        prod_cod = None
        if d.stock_inventario and d.stock_inventario.producto_color:
            prod_cod = d.stock_inventario.producto_color.producto_codigo
        detalles.append({
            "id": d.id,
            "stock_inventario_id": d.stock_inventario_id,
            "producto_codigo": prod_cod,
            "producto_nombre": d.producto_nombre,
            "color_nombre": d.color_nombre,
            "talla_nombre": d.talla_nombre,
            "cantidad": d.cantidad,
            "precio_unitario": d.precio_unitario,
            "subtotal": d.subtotal,
        })

    cli_nom = None
    cli_email = None
    cli_tel = None
    if o.cliente:
        cli_tel = o.cliente.telefono
        if o.cliente.user:
            u = o.cliente.user
            cli_nom = f"{u.name} {u.apellido or ''}".strip()
            cli_email = u.email
    cli_nom = o.comprador_nombre or cli_nom

    envio_dict = None
    if o.envios:
        env = o.envios[0]
        envio_dict = {
            "id": env.id,
            "orden_venta_id": env.orden_venta_id,
            "direccion": env.direccion,
            "ciudad": env.ciudad,
            "referencia": env.referencia,
            "costo": env.costo,
            "estado": env.estado,
            "fecha": env.fecha,
            "created_at": env.created_at,
            "cliente_nombre": cli_nom,
            "yango_tracking_code": getattr(env, "yango_tracking_code", None),
            "yango_tracking_url": getattr(env, "yango_tracking_url", None),
            "delivery_conductor": getattr(env, "delivery_conductor", None),
            "token_seguimiento": getattr(env, "token_seguimiento", None),
            "tracking_activo": getattr(env, "tracking_activo", True),
            "repartidor_lat": float(env.repartidor_lat) if env.repartidor_lat else None,
            "repartidor_lon": float(env.repartidor_lon) if env.repartidor_lon else None,
            "repartidor_actualizado_en": env.repartidor_actualizado_en.isoformat() if env.repartidor_actualizado_en else None,
            "distancia_km": float(env.distancia_km) if env.distancia_km else None,
            "minutos_estimados": env.minutos_estimados,
        }

    cambios = []
    for item in o.cambios_solicitados:
        replacement = item.nuevo_stock
        replacement_product = replacement.producto_color.producto if replacement and replacement.producto_color else None
        replacement_color = replacement.producto_color.color if replacement and replacement.producto_color else None
        cambios.append({
            "id": item.id,
            "tipo": item.tipo,
            "motivo": item.motivo,
            "descripcion_problema": item.descripcion_problema,
            "estado": item.estado,
            "respuesta_encargado": item.respuesta_encargado,
            "fecha_programada": item.fecha_programada,
            "producto_original": item.detalle_venta.producto_nombre if item.detalle_venta else None,
            "talla_original": item.detalle_venta.talla_nombre if item.detalle_venta else None,
            "color_original": item.detalle_venta.color_nombre if item.detalle_venta else None,
            "producto_reemplazo": replacement_product.nombre if replacement_product else None,
            "talla_reemplazo": replacement.talla.nombre if replacement and replacement.talla else None,
            "color_reemplazo": replacement_color.nombre if replacement_color else None,
            "precio_original": item.precio_original,
            "precio_nuevo": item.precio_nuevo,
            "monto_adicional": item.monto_adicional,
            "monto_reembolsado": item.monto_reembolsado,
            "nota_venta_numero": item.nota_venta_numero,
            "orden_ajuste_id": item.orden_ajuste_id,
            "reponer_prenda_original": item.reponer_prenda_original,
            "created_at": item.created_at,
        })

    return {
        "id": o.id,
        "fecha": o.fecha,
        "estado": o.estado,
        "total": o.total,
        "subtotal_productos": o.subtotal_productos,
        "descuento_promociones": o.descuento_promociones,
        "descuento_codigo": o.descuento_codigo,
        "codigo_descuento": o.codigo_descuento,
        "porcentaje_codigo": o.porcentaje_codigo,
        "tipo_venta": o.tipo_venta,
        "cambio_devolucion_id": o.cambio_ajuste.id if o.cambio_ajuste else None,
        "liquidacion_tipo": (
            "cobro" if o.cambio_ajuste and Decimal(o.cambio_ajuste.monto_adicional or 0) > 0
            else "reembolso" if o.cambio_ajuste and Decimal(o.cambio_ajuste.monto_reembolsado or 0) > 0
            else "sin_ajuste" if o.cambio_ajuste else None
        ),
        "metodo_pago": o.metodo_pago,
        "tipo_entrega": o.tipo_entrega,
        "fecha_recojo": o.fecha_recojo,
        "hora_recojo": o.hora_recojo,
        "pago_id": pago.id if pago else None,
        "pago_estado": pago.estado if pago else None,
        "recibo_numero": pago.recibo_numero if pago else None,
        "pago_email_confirmado": bool(pago and pago.email_confirmacion_enviado_at),
        "payment_operation_uuid": pago.operation_uuid if pago else None,
        "paypal_order_id": pago.paypal_order_id if pago else None,
        "paypal_capture_id": pago.paypal_capture_id if pago else None,
        "paypal_refund_id": pago.paypal_refund_id if pago else None,
        "refund_reason": pago.refund_reason if pago else None,
        "ticket_numero": o.ticket_numero,
        "codigo_cliente": o.codigo_cliente,
        "comprador_nombre": o.comprador_nombre,
        "comprador_ci": o.comprador_ci,
        "cliente_nombre": cli_nom,
        "cliente_email": cli_email,
        "cliente_telefono": cli_tel,
        "sucursal_ciudad": o.sucursal.ciudad if o.sucursal else None,
        "sucursal_nombre": o.sucursal.nombre if o.sucursal else None,
        "sucursal_direccion": o.sucursal.direccion if o.sucursal else None,
        "sucursal_id": o.sucursal_id,
        "detalles": detalles,
        "envio": envio_dict,
        "cambios_devoluciones": cambios,
        "created_at": o.created_at,
    }


@router.get("", response_model=list[OrdenVentaResponse], summary="Listar ventas combinadas (Administración y Clientes)")
async def list_ventas(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista combinada de ventas. Si es cliente retorna sus compras, si es staff retorna todas."""
    if current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente:
            return []
        ventas = _client_orders_query(db, current_user, cliente).order_by(OrdenVenta.created_at.desc()).all()
        return [_serialize_orden(v) for v in ventas]

    ventas = scope_by_sucursal(
        db.query(OrdenVenta), db, current_user, OrdenVenta.sucursal_id
    ).order_by(OrdenVenta.created_at.desc()).all()
    return [_serialize_orden(v) for v in ventas]


@router.get("/mias", summary="Historial de compras del cliente (v5 sección 21)")
async def list_my_purchases(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Devuelve las compras del cliente separadas en:
    - compras_carrito: Compras en línea vía carrito.
    - compras_presenciales: Compras presenciales en tienda física.
    """
    cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
    if not cliente:
        return {"compras_carrito": [], "compras_presenciales": []}

    ventas = _client_orders_query(db, current_user, cliente).order_by(OrdenVenta.created_at.desc()).all()

    carrito_list = [_serialize_orden(v) for v in ventas if v.tipo_venta == "en linea"]
    presenciales_list = [_serialize_orden(v) for v in ventas if v.tipo_venta == "presencial"]

    return {
        "compras_carrito": carrito_list,
        "compras_presenciales": presenciales_list,
    }


@router.get("/mis-compras", response_model=list[OrdenVentaResponse], summary="Historial de compras para aplicación móvil y web")
async def list_mis_compras(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Devuelve la lista unificada de todas las órdenes de venta del cliente actual."""
    cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
    if not cliente:
        return []

    ventas = _client_orders_query(db, current_user, cliente).order_by(OrdenVenta.created_at.desc()).all()
    return [_serialize_orden(v) for v in ventas]


@router.get("/{venta_id}", response_model=OrdenVentaResponse, summary="Detalle de venta")
async def get_venta(
    venta_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Detalle completo de una orden de venta."""
    venta_query = db.query(OrdenVenta).filter(OrdenVenta.id == venta_id)
    venta = scope_by_sucursal(venta_query, db, current_user, OrdenVenta.sucursal_id).first()
    if not venta:
        raise NotFoundException(f"Venta con ID {venta_id} no encontrada.")
    if current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente or venta.codigo_cliente != cliente.codigo:
            raise NotFoundException(f"Venta con ID {venta_id} no encontrada.")
    return _serialize_orden(venta)


@router.post(
    "/presencial",
    response_model=OrdenVentaResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar venta presencial en caja",
    dependencies=[Depends(require_plan_capability("ventas"))],
)
async def create_venta_presencial(
    payload: VentaPresencialCreate,
    current_user: User = Depends(require_permission("productos.editar")),
    db: Session = Depends(get_db),
):
    """
    Registra una compra presencial desde caja:
    - Descuenta existencias en inventario atómicamente.
    - Genera la OrdenVenta y DetalleVenta.
    - Registra el Pago en caja inmediatamente.
    """
    cliente = None
    comprador_nombre = payload.comprador_nombre.strip() if payload.comprador_nombre else None
    comprador_ci = payload.comprador_ci.strip() if payload.comprador_ci else None
    if payload.codigo_cliente:
        cliente = (
            db.query(Cliente)
            .join(User, Cliente.user_id == User.id)
            .outerjoin(UserEmpresa, UserEmpresa.user_id == User.id)
            .filter(
                Cliente.codigo == payload.codigo_cliente,
                or_(
                    User.empresa_id == current_user.empresa_id,
                    (UserEmpresa.empresa_id == current_user.empresa_id) & UserEmpresa.active.is_(True),
                ),
            )
            .first()
        )
        if not cliente:
            raise NotFoundException("El cliente no existe o no pertenece al tenant activo.")
        comprador_nombre = cliente.nombre
        comprador_ci = cliente.user.ci if cliente.user else None

    now = datetime.now()
    total_venta = Decimal("0.00")
    detalles_orden = []
    promo_service = PromocionService(db)
    cupon = promo_service.validar_codigo(current_user.empresa_id, payload.codigo_descuento, "presencial", lock=True)
    subtotal_original = Decimal("0.00")
    descuento_promociones = Decimal("0.00")

    sucursal_id_detectada = payload.sucursal_id
    if payload.sucursal_id:
        get_scoped_sucursal(db, current_user, payload.sucursal_id)

    for item in payload.items:
        stock_query = (
            db.query(StockInventario)
            .filter(StockInventario.id == item.stock_inventario_id)
        )
        stock = scope_by_sucursal(
            stock_query, db, current_user, StockInventario.sucursal_id
        ).with_for_update(of=StockInventario).first()
        if not stock:
            db.rollback()
            raise NotFoundException(f"Inventario #{item.stock_inventario_id} no encontrado.")

        # Si se seleccionó una sucursal en caja y el stock pertenece a otra, buscar existencia local
        if payload.sucursal_id and stock.sucursal_id != payload.sucursal_id:
            stock_local = (
                db.query(StockInventario)
                .filter(
                    StockInventario.producto_color_id == stock.producto_color_id,
                    StockInventario.talla_id == stock.talla_id,
                    StockInventario.sucursal_id == payload.sucursal_id,
                )
                .with_for_update(of=StockInventario)
                .first()
            )
            if stock_local:
                stock = stock_local

        if not sucursal_id_detectada:
            sucursal_id_detectada = stock.sucursal_id

        if stock.cantidad < item.cantidad:
            disponible = stock.cantidad
            solicitado = item.cantidad
            db.rollback()
            raise BadRequestException(
                "Prenda no disponible: Stock insuficiente en sucursal. "
                f"Disponible: {disponible}, solicitado: {solicitado}. Por favor revisa otras variantes o sucursales."
            )

        stock.cantidad -= item.cantidad

        precio = Decimal("0.00")
        prod_nom = "Prenda"
        col_nom = None
        talla_nom = None
        if stock.producto_color:
            col_nom = stock.producto_color.color.nombre if stock.producto_color.color else None
            if stock.producto_color.producto:
                precio_original, precio, _ = promo_service.precio_stock(stock)
                subtotal_original += precio_original * item.cantidad
                descuento_promociones += (precio_original - precio) * item.cantidad
                prod_nom = stock.producto_color.producto.nombre
        if stock.talla:
            talla_nom = stock.talla.nombre

        subtotal = precio * item.cantidad
        total_venta += subtotal

        detalles_orden.append({
            "stock_inventario_id": stock.id,
            "producto_nombre": prod_nom,
            "color_nombre": col_nom,
            "talla_nombre": talla_nom,
            "cantidad": item.cantidad,
            "precio_unitario": precio,
            "subtotal": subtotal,
        })

    # Cálculo de vuelto y validación de efectivo si aplica
    descuento_codigo = promo_service._money(total_venta * Decimal(cupon.porcentaje if cupon else 0) / Decimal(100))
    total_venta = promo_service._money(total_venta - descuento_codigo)
    if cupon:
        cupon.usos_actual += 1

    metodo = (payload.metodo_pago or "efectivo").lower()
    recibido = payload.efectivo_recibido or total_venta
    cambio = Decimal("0.00")
    if metodo == "efectivo" and recibido >= total_venta:
        cambio = recibido - total_venta

    ticket_num = f"TCK-{now.strftime('%Y%m%d%H%M%S')}"

    orden = OrdenVenta(
        client_operation_uuid=payload.operation_uuid,
        fecha=now.date(),
        estado="pagada",
        total=total_venta,
        subtotal_productos=promo_service._money(subtotal_original),
        descuento_promociones=promo_service._money(descuento_promociones),
        descuento_codigo=descuento_codigo,
        codigo_descuento=cupon.codigo if cupon else None,
        porcentaje_codigo=cupon.porcentaje if cupon else 0,
        tipo_venta="presencial",
        metodo_pago=metodo,
        ticket_numero=ticket_num,
        efectivo_recibido=recibido if metodo == "efectivo" else total_venta,
        cambio_devuelto=cambio if metodo == "efectivo" else Decimal("0.00"),
        codigo_cliente=cliente.codigo if cliente else None,
        comprador_nombre=comprador_nombre,
        comprador_ci=comprador_ci,
        sucursal_id=sucursal_id_detectada or payload.sucursal_id,
    )
    db.add(orden)
    db.flush()

    for d in detalles_orden:
        dv = DetalleVenta(
            orden_venta_id=orden.id,
            stock_inventario_id=d["stock_inventario_id"],
            producto_nombre=d["producto_nombre"],
            color_nombre=d["color_nombre"],
            talla_nombre=d["talla_nombre"],
            cantidad=d["cantidad"],
            precio_unitario=d["precio_unitario"],
            subtotal=d["subtotal"],
        )
        db.add(dv)

    pago = Pago(
        orden_venta_id=orden.id,
        empresa_id=orden.sucursal.empresa_id if orden.sucursal else current_user.empresa_id,
        monto=total_venta,
        tipo_pago="en caja",
        metodo_pago=metodo,
        estado="aprobado",
    )
    db.add(pago)

    db.commit()
    db.refresh(orden)
    db.refresh(pago)
    PagoService(db).confirmar(pago)

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Registró venta presencial #{orden.id} ({ticket_num}) en caja por Bs {orden.total} [{metodo.upper()}] para '{comprador_nombre}' (CI {comprador_ci})",
        module="ventas",
    )

    return _serialize_orden(orden)


@router.delete("/{venta_id}", summary="Eliminar venta")
async def delete_venta(
    venta_id: int,
    current_user: User = Depends(require_permission("ventas.eliminar")),
    db: Session = Depends(get_db),
):
    """Eliminar un registro de orden de venta."""
    orden_query = db.query(OrdenVenta).filter(OrdenVenta.id == venta_id)
    orden = scope_by_sucursal(orden_query, db, current_user, OrdenVenta.sucursal_id).first()
    if not orden:
        raise NotFoundException(f"Venta con ID {venta_id} no encontrada.")

    db.delete(orden)
    db.commit()

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Eliminó la venta #{venta_id}",
        module="ventas",
    )
    return {"message": f"Venta #{venta_id} eliminada exitosamente."}
