"""
Reservas endpoints (v5 sección 16).
Incluye transacción atómica con descuento automático de inventario.
"""
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, status, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models.user import User
from app.models.cliente import Cliente
from app.models.reserva import Reserva, DetalleReserva
from app.models.stock_inventario import StockInventario
from app.models.orden_venta import OrdenVenta, DetalleVenta
from app.schemas.reserva import (
    ExpirarReservasResponse,
    ReservaCreate,
    ReservaResponse,
    ReservaEstadoUpdate,
    DetalleReservaResponse,
)
from app.api.deps import get_current_user, require_permission, require_plan_capability
from app.core.exceptions import NotFoundException, BadRequestException
from app.services.bitacora_service import BitacoraService
from app.services.tenant_scope import get_scoped_sucursal, scope_by_sucursal
from app.services.agenda_service import hour_blocks
from app.services.promocion_service import PromocionService

router = APIRouter(prefix="/reservas", tags=["Reservas"])


def _hour_blocks(opening: time, closing: time) -> list[tuple[time, time]]:
    return hour_blocks(opening, closing)


def _is_slot_conflict(exc: IntegrityError) -> bool:
    detail = str(getattr(exc, "orig", exc)).lower()
    return "uq_reserva_bloque_activo" in detail or "sucursal_id, fecha, hora" in detail


@router.get("/disponibilidad", summary="Bloques horarios disponibles para una sucursal")
async def disponibilidad_reserva(
    sucursal_id: int = Query(...),
    fecha: date = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sucursal = get_scoped_sucursal(db, current_user, sucursal_id)
    hoy = date.today()
    if fecha < hoy or fecha > hoy + timedelta(days=3):
        raise BadRequestException("La fecha debe estar entre hoy y los próximos 3 días.")
    occupied = {
        row[0]
        for row in db.query(Reserva.hora).filter(
            Reserva.sucursal_id == sucursal_id,
            Reserva.fecha == fecha,
            Reserva.estado == "pendiente",
        ).all()
    }
    now = datetime.now()
    return {
        "sucursal_id": sucursal.id,
        "fecha": fecha,
        "hora_apertura": sucursal.hora_apertura,
        "hora_cierre": sucursal.hora_cierre,
        "bloques": [
            {
                "inicio": start.strftime("%H:%M"),
                "fin": end.strftime("%H:%M"),
                "disponible": start not in occupied
                and not (fecha == hoy and datetime.combine(fecha, start) <= now),
            }
            for start, end in _hour_blocks(sucursal.hora_apertura, sucursal.hora_cierre)
        ],
    }


def _serialize_reserva(res: Reserva) -> dict:
    detalles_resp = []
    for d in res.detalles:
        si = d.stock_inventario
        prod_cod = None
        prod_nom = None
        col_nom = None
        talla_nom = None
        foto = None
        if si:
            if si.producto_color:
                col_nom = si.producto_color.color.nombre if si.producto_color.color else None
                if si.producto_color.producto:
                    prod_cod = si.producto_color.producto.codigo
                    prod_nom = si.producto_color.producto.nombre
                    foto = si.producto_color.producto.foto
            if si.talla:
                talla_nom = si.talla.nombre

        detalles_resp.append({
            "id": d.id,
            "stock_inventario_id": d.stock_inventario_id,
            "cantidad": d.cantidad,
            "producto_codigo": prod_cod,
            "producto_nombre": prod_nom,
            "color_nombre": col_nom,
            "talla_nombre": talla_nom,
            "foto": foto,
        })

    cli_name = f"{res.cliente.user.name} {res.cliente.user.apellido or ''}".strip() if (res.cliente and res.cliente.user) else None

    return {
        "id": res.id,
        "fecha": res.fecha,
        "hora": res.hora,
        "estado": res.estado,
        "codigo_cliente": res.codigo_cliente,
        "cliente_nombre": cli_name,
        "sucursal_id": res.sucursal_id,
        "sucursal_ciudad": res.sucursal.ciudad if res.sucursal else None,
        "sucursal_direccion": res.sucursal.direccion if res.sucursal else None,
        "detalles": detalles_resp,
        "created_at": res.created_at,
        "tipo": res.tipo,
        "orden_venta_id": res.orden_venta_id,
    }


def expirar_reservas_vencidas_db(
    db: Session,
    current_user: User | None = None,
    sucursal_id: int | None = None,
    empresa_id: int | None = None,
) -> tuple[int, int]:
    """
    Expira reservas en estado 'pendiente' cuya fecha límite sea anterior a hoy.
    Restituye automáticamente las existencias a StockInventario y audita.
    Devuelve (reservas_procesadas, unidades_stock_restituidas).
    """
    from app.models.sucursal import Sucursal

    hoy = date.today()
    q = db.query(Reserva).filter(
        Reserva.estado == "pendiente",
        Reserva.fecha < hoy,
    )
    if sucursal_id:
        q = q.filter(Reserva.sucursal_id == sucursal_id)
    elif empresa_id:
        q = q.join(Sucursal, Reserva.sucursal_id == Sucursal.id).filter(
            Sucursal.empresa_id == empresa_id
        )
    elif current_user and not current_user.is_platform_admin and current_user.empresa_id:
        q = q.join(Sucursal, Reserva.sucursal_id == Sucursal.id).filter(
            Sucursal.empresa_id == current_user.empresa_id
        )

    vencidas = q.all()
    if not vencidas:
        return 0, 0

    count_procesadas = 0
    total_unidades = 0

    for r in vencidas:
        unidades_reserva = 0
        for d in r.detalles:
            stock = (
                db.query(StockInventario)
                .filter(StockInventario.id == d.stock_inventario_id)
                .with_for_update(of=StockInventario)
                .first()
            )
            if stock:
                stock.cantidad += d.cantidad
                total_unidades += d.cantidad
                unidades_reserva += d.cantidad

        r.estado = "vencida"
        count_procesadas += 1

        BitacoraService.registrar(
            db=db,
            user=current_user,
            action=f"Reserva #{r.id} expiró por superar plazo máximo de 3 días; se restituyeron {unidades_reserva} prenda(s) a inventario.",
            module="reservas",
        )

    db.commit()
    return count_procesadas, total_unidades


@router.post(
    "/expirar-vencidas",
    response_model=ExpirarReservasResponse,
    summary="Expirar reservas vencidas y restituir stock (REQ-058)",
    dependencies=[Depends(require_permission("reservas.editar"))],
)
async def expirar_reservas_endpoint(
    sucursal_id: Optional[int] = Query(None, description="Filtrar por sucursal específica"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Detecta reservas pendientes cuya fecha límite ya expiró (más de 3 días),
    las marca como 'vencida', devuelve automáticamente las prendas a inventario
    y registra la auditoría correspondiente.
    """
    procesadas, stock_restituido = expirar_reservas_vencidas_db(
        db=db,
        current_user=current_user,
        sucursal_id=sucursal_id,
        empresa_id=current_user.empresa_id if not current_user.is_platform_admin else None,
    )
    return ExpirarReservasResponse(
        procesadas=procesadas,
        stock_restituido=stock_restituido,
        mensaje=f"Se procesaron {procesadas} reserva(s) vencida(s) y se restituyeron {stock_restituido} prenda(s) a inventario.",
    )


@router.get("", response_model=list[ReservaResponse], summary="Listar reservas (Administración y Clientes)")
async def list_reservas(
    sucursal_id: Optional[int] = Query(None, description="Filtrar por ID de sucursal"),
    estado: Optional[str] = Query(None, description="Filtrar por estado ('pendiente', 'completada', 'cancelada', 'vencida')"),
    search: Optional[str] = Query(None, description="Búsqueda por cliente, prenda o ID"),
    fecha_desde: Optional[date] = Query(None, description="Fecha de retiro inicial"),
    fecha_hasta: Optional[date] = Query(None, description="Fecha de retiro final"),
    tipo: Optional[str] = Query(None, description="Tipo de reserva (ej. reserva_prenda, recojo_orden)"),
    empresa_id: Optional[int] = Query(None, description="Filtrar por tenant (solo superadmin)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Listado de reservas con filtros para administración y clientes (REQ-058)."""
    # Auto-expirar reservas vencidas para staff/administración garantizando datos en tiempo real
    if current_user.role != "cliente":
        expirar_reservas_vencidas_db(
            db=db,
            current_user=current_user,
            sucursal_id=sucursal_id,
            empresa_id=empresa_id or (current_user.empresa_id if not current_user.is_platform_admin else None),
        )

    if current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente:
            return []
        q = db.query(Reserva).filter(Reserva.codigo_cliente == cliente.codigo)
        q = scope_by_sucursal(q, db, current_user, Reserva.sucursal_id)
    else:
        q = scope_by_sucursal(db.query(Reserva), db, current_user, Reserva.sucursal_id)
        if empresa_id and current_user.is_platform_admin:
            from app.models.sucursal import Sucursal
            q = q.join(Sucursal, Reserva.sucursal_id == Sucursal.id).filter(Sucursal.empresa_id == empresa_id)

    if sucursal_id:
        q = q.filter(Reserva.sucursal_id == sucursal_id)
    if estado:
        q = q.filter(Reserva.estado == estado)
    if tipo:
        q = q.filter(Reserva.tipo == tipo)
    if fecha_desde:
        q = q.filter(Reserva.fecha >= fecha_desde)
    if fecha_hasta:
        q = q.filter(Reserva.fecha <= fecha_hasta)

    if search:
        s_clean = search.strip()
        from app.models.producto import Producto
        from app.models.producto_color import ProductoColor

        q = (
            q.outerjoin(Cliente, Reserva.codigo_cliente == Cliente.codigo)
            .outerjoin(User, Cliente.user_id == User.id)
            .outerjoin(DetalleReserva, Reserva.id == DetalleReserva.reserva_id)
            .outerjoin(StockInventario, DetalleReserva.stock_inventario_id == StockInventario.id)
            .outerjoin(ProductoColor, StockInventario.producto_color_id == ProductoColor.id)
            .outerjoin(Producto, ProductoColor.producto_codigo == Producto.codigo)
        )
        from typing import Any
        filtros: list[Any] = [
            Reserva.codigo_cliente.ilike(f"%{s_clean}%"),
            User.name.ilike(f"%{s_clean}%"),
            User.apellido.ilike(f"%{s_clean}%"),
            User.email.ilike(f"%{s_clean}%"),
            Producto.nombre.ilike(f"%{s_clean}%"),
            Producto.codigo.ilike(f"%{s_clean}%"),
        ]
        if s_clean.isdigit():
            filtros.append(Reserva.id == int(s_clean))
        q = q.filter(or_(*filtros)).distinct()

    reservas = q.order_by(Reserva.created_at.desc()).all()
    return [_serialize_reserva(r) for r in reservas]


@router.get("/mias", response_model=list[ReservaResponse], summary="Historial de reservas del cliente")
async def list_my_reservas(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene el historial de reservas personales del cliente autenticado."""
    codigos = set()
    cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
    if not cliente and current_user.ci:
        cliente = db.query(Cliente).join(User).filter(User.ci == current_user.ci).first()
        if cliente:
            cliente.user_id = current_user.id
            db.commit()

    if not cliente:
        clean_ci = current_user.ci or str(current_user.id).zfill(4)
        cod = f"CL-{current_user.name[:2].upper()}-{clean_ci[-4:]}"
        cliente = Cliente(
            codigo=cod,
            user_id=current_user.id,
            telefono=current_user.telefono or "00000000",
            direccion=current_user.direccion or "Sin dirección",
        )
        db.add(cliente)
        db.commit()
        db.refresh(cliente)

    codigos.add(cliente.codigo)
    if current_user.ci:
        for c in db.query(Cliente).join(User).filter(User.ci == current_user.ci).all():
            codigos.add(c.codigo)

    reservas_query = db.query(Reserva).filter(Reserva.codigo_cliente.in_(list(codigos)))
    reservas = scope_by_sucursal(
        reservas_query, db, current_user, Reserva.sucursal_id
    ).order_by(Reserva.created_at.desc()).all()
    return [_serialize_reserva(r) for r in reservas]



@router.get("/elegibilidad", summary="Verificar si el cliente actual puede realizar reservas")
async def verificar_elegibilidad_reserva(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Informa el historial del cliente sin bloquear el requisito básico de reserva."""
    from app.models.orden_venta import OrdenVenta
    from app.models.carrito import Carrito
    from sqlalchemy import or_, func

    if current_user.role in ["administrador", "encargado"]:
        return {
            "puede_reservar": True,
            "compras_previas": 99,
            "mensaje": "Cuenta de administración/gestión habilitada para reservas.",
        }

    cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
    if not cliente:
        return {
            "puede_reservar": True,
            "compras_previas": 0,
            "mensaje": "Tu cuenta puede reservar prendas. El horario y el stock se validarán al confirmar.",
        }

    compras_count = (
        db.query(OrdenVenta)
        .filter(
            OrdenVenta.codigo_cliente == cliente.codigo,
            func.lower(OrdenVenta.estado).in_(["pagado", "pagada", "completado", "completada", "entregado", "entregada"]),
        )
        .count()
    )

    mensaje = (
        "Tu cuenta puede reservar prendas; además, ya cuenta con compras confirmadas."
        if compras_count > 0
        else "Tu cuenta puede reservar prendas. No es necesario haber comprado antes."
    )

    return {
        "puede_reservar": True,
        "compras_previas": compras_count,
        "mensaje": mensaje,
    }


@router.post(
    "",
    response_model=ReservaResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear reserva",
    dependencies=[Depends(require_plan_capability("reservas")), Depends(require_permission("reservas.crear"))],
)
async def create_reserva(
    payload: ReservaCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Crea una reserva desde el catálogo y descuenta stock de forma atómica."""
    from app.services.notificacion_service import NotificacionService

    # Obtener o registrar cliente si no existe
    cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
    if not cliente:
        clean_ci = current_user.ci or str(current_user.id).zfill(4)
        cod = f"CL-{current_user.name[:2].upper()}-{clean_ci[-4:]}"
        cliente = Cliente(
            codigo=cod,
            user_id=current_user.id,
            telefono=current_user.telefono or "00000000",
            direccion=current_user.direccion or "Sin dirección",
        )
        db.add(cliente)
        db.flush()

    sucursal = get_scoped_sucursal(db, current_user, payload.sucursal_id)

    # La recogida puede programarse desde hoy hasta un máximo de 3 días.
    hoy = date.today()
    max_fecha = hoy + timedelta(days=3)
    fecha_reserva = payload.fecha_limite or max_fecha
    if fecha_reserva < hoy or fecha_reserva > max_fecha:
        raise BadRequestException(
            f"La fecha de retiro no puede ser anterior a hoy ni superar el plazo máximo de 3 días ({max_fecha.strftime('%d/%m/%Y')})."
        )

    valid_starts = {start for start, _ in _hour_blocks(sucursal.hora_apertura, sucursal.hora_cierre)}
    hora_retiro = payload.hora_retiro or min(valid_starts, default=sucursal.hora_apertura)
    if hora_retiro not in valid_starts:
        raise BadRequestException(
            "La hora de retiro debe iniciar un bloque completo de una hora dentro del horario de atención de la sucursal "
            f"({sucursal.hora_apertura.strftime('%H:%M')} - {sucursal.hora_cierre.strftime('%H:%M')})."
        )
    if fecha_reserva == hoy and hora_retiro <= datetime.now().time():
        raise BadRequestException("La hora de retiro debe ser posterior a la hora actual.")

    reserva = Reserva(
        client_operation_uuid=payload.operation_uuid,
        fecha=fecha_reserva,
        hora=hora_retiro,
        estado="pendiente",
        codigo_cliente=cliente.codigo,
        sucursal_id=payload.sucursal_id,
    )
    db.add(reserva)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if _is_slot_conflict(exc):
            raise BadRequestException(
                "El bloque horario acaba de ser ocupado por otra persona. Selecciona otro horario disponible."
            ) from exc
        raise

    # Descuento atómico de stock por cada ítem
    for item in payload.items:
        stock = (
            db.query(StockInventario)
            .filter(StockInventario.id == item.stock_inventario_id)
            .with_for_update(of=StockInventario)
            .first()
        )
        if not stock:
            db.rollback()
            raise NotFoundException(f"Inventario con ID {item.stock_inventario_id} no encontrado.")

        if stock.sucursal_id != payload.sucursal_id:
            db.rollback()
            raise BadRequestException(
                "Todos los artículos de la reserva deben pertenecer a la sucursal elegida."
            )

        if stock.cantidad < item.cantidad:
            disponible = stock.cantidad
            solicitado = item.cantidad
            stock_id = item.stock_inventario_id
            db.rollback()
            raise BadRequestException(
                f"Prenda no disponible: Stock insuficiente para el ítem {stock_id}. "
                f"Disponible: {disponible}, solicitado: {solicitado}. Por favor revisa otras variantes o sucursales."
            )

        stock.cantidad -= item.cantidad
        detalle = DetalleReserva(
            reserva_id=reserva.id,
            stock_inventario_id=stock.id,
            cantidad=item.cantidad,
        )
        db.add(detalle)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_slot_conflict(exc):
            raise BadRequestException(
                "El bloque horario acaba de ser ocupado por otra persona. Selecciona otro horario disponible."
            ) from exc
        raise
    db.refresh(reserva)

    # Las operaciones sincronizadas reciben un único aviso definitivo desde
    # /sync (confirmada o conflicto). Evita duplicar la notificación de éxito.
    if not payload.operation_uuid:
        try:
            notif_srv = NotificacionService(db)
            notif_srv.crear_notificacion(
                user_id=current_user.id,
                tipo="recordatorio_reserva",
                titulo="Reserva Registrada en StyleStore",
                mensaje=f"Tu reserva #{reserva.id} ha sido registrada. Vigencia hasta el {reserva.fecha.strftime('%d/%m/%Y')} para retirar en sucursal.",
                url_accion="/reservas",
                enviar_email=True,
                empresa_id=current_user.empresa_id,
            )
            notif_srv.notificar_nueva_reserva(reserva)
        except Exception:
            pass

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Registró la reserva #{reserva.id} para la sucursal #{reserva.sucursal_id}",
        module="reservas",
    )

    return _serialize_reserva(reserva)


@router.post(
    "/{reserva_id}/enviar-a-caja",
    summary="Convertir reserva en orden pendiente de cobro",
    dependencies=[Depends(require_permission("reservas.editar"))],
)
async def enviar_reserva_a_caja(
    reserva_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reserva = scope_by_sucursal(
        db.query(Reserva).filter(Reserva.id == reserva_id),
        db,
        current_user,
        Reserva.sucursal_id,
    ).with_for_update(of=Reserva).first()
    if not reserva:
        raise NotFoundException("Reserva no encontrada en el tenant activo.")
    if reserva.orden_venta_id:
        orden = db.query(OrdenVenta).filter(OrdenVenta.id == reserva.orden_venta_id).first()
        return {"reserva_id": reserva.id, "orden_venta_id": reserva.orden_venta_id, "estado": reserva.estado, "total": float(orden.total if orden else 0)}
    if reserva.estado != "pendiente":
        raise BadRequestException("Solo una reserva pendiente puede enviarse a caja.")

    promo = PromocionService(db)
    total = Decimal("0.00")
    lineas = []
    for detalle in reserva.detalles:
        stock = detalle.stock_inventario
        if not stock or stock.sucursal_id != reserva.sucursal_id:
            raise BadRequestException("La reserva contiene una existencia inválida.")
        _, precio, _ = promo.precio_stock(stock)
        producto = stock.producto_color.producto
        color = stock.producto_color.color
        subtotal = precio * detalle.cantidad
        total += subtotal
        lineas.append((detalle, producto, color, stock.talla, precio, subtotal))

    orden = OrdenVenta(
        fecha=date.today(),
        estado="pendiente_pago",
        total=promo._money(total),
        subtotal_productos=promo._money(total),
        tipo_venta="presencial",
        metodo_pago=None,
        inventario_descontado=True,
        codigo_cliente=reserva.codigo_cliente,
        comprador_nombre=reserva.cliente.nombre if reserva.cliente else None,
        sucursal_id=reserva.sucursal_id,
    )
    db.add(orden)
    db.flush()
    for detalle, producto, color, talla, precio, subtotal in lineas:
        db.add(DetalleVenta(
            orden_venta_id=orden.id,
            stock_inventario_id=detalle.stock_inventario_id,
            producto_nombre=producto.nombre,
            color_nombre=color.nombre if color else None,
            talla_nombre=talla.nombre if talla else None,
            cantidad=detalle.cantidad,
            precio_unitario=precio,
            subtotal=subtotal,
        ))
    reserva.orden_venta_id = orden.id
    reserva.estado = "pendiente_pago"
    db.commit()
    return {"reserva_id": reserva.id, "orden_venta_id": orden.id, "estado": reserva.estado, "total": float(orden.total)}


@router.post("/{reserva_id}/cancelar", response_model=ReservaResponse, summary="Cancelar reserva y restituir stock")
async def cancelar_reserva(
    reserva_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancela una reserva y devuelve automáticamente el stock reservado a inventario."""
    reserva_query = db.query(Reserva).filter(Reserva.id == reserva_id)
    reserva = scope_by_sucursal(
        reserva_query, db, current_user, Reserva.sucursal_id
    ).first()
    if not reserva:
        raise NotFoundException(f"Reserva con ID {reserva_id} no encontrada.")

    # Si es cliente, validar que sea el dueño de la reserva
    if current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente or reserva.codigo_cliente != cliente.codigo:
            raise BadRequestException("No tienes permiso para cancelar esta reserva.")

    if reserva.estado == "cancelada":
        raise BadRequestException("Esta reserva ya ha sido cancelada previamente.")

    if reserva.estado in ["completada", "entregada"]:
        raise BadRequestException("No se puede cancelar una reserva que ya ha sido completada o retirada.")

    # Restituir stock atómicamente si estaba pendiente
    if reserva.estado == "pendiente":
        for d in reserva.detalles:
            stock = (
                db.query(StockInventario)
                .filter(StockInventario.id == d.stock_inventario_id)
                .with_for_update(of=StockInventario)
                .first()
            )
            if stock:
                stock.cantidad += d.cantidad

    reserva.estado = "cancelada"
    db.commit()
    db.refresh(reserva)

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Canceló la reserva #{reserva.id} y restituyó el stock de {len(reserva.detalles)} prenda(s)",
        module="reservas",
    )

    try:
        from app.services.notificacion_service import NotificacionService
        notif_srv = NotificacionService(db)
        notif_srv.crear_notificacion(
            user_id=current_user.id,
            tipo="cancelacion_reserva",
            titulo="Reserva Cancelada",
            mensaje=f"Tu reserva #{reserva.id} ha sido cancelada exitosamente y el stock ha sido liberado.",
            url_accion="/cuenta/mis-reservas",
            enviar_email=False,
            empresa_id=reserva.sucursal.empresa_id,
        )
    except Exception:
        pass

    return _serialize_reserva(reserva)


@router.patch("/{reserva_id}/estado", response_model=ReservaResponse, summary="Actualizar estado de reserva")
async def update_reserva_estado(
    reserva_id: int,
    payload: ReservaEstadoUpdate,
    current_user: User = Depends(require_permission("reservas.editar")),
    db: Session = Depends(get_db),
):
    """Actualiza el estado de una reserva (ej. pendiente -> completada o cancelada)."""
    reserva_query = db.query(Reserva).filter(Reserva.id == reserva_id)
    reserva = scope_by_sucursal(
        reserva_query, db, current_user, Reserva.sucursal_id
    ).first()
    if not reserva:
        raise NotFoundException(f"Reserva con ID {reserva_id} no encontrada.")
    if payload.estado == "completada" and reserva.orden_venta_id:
        orden = db.query(OrdenVenta).filter(OrdenVenta.id == reserva.orden_venta_id).first()
        if not orden or orden.estado != "pagada":
            raise BadRequestException("La reserva debe pagarse en caja antes de entregar las prendas.")

    # Si se cancela o expira la reserva, restituir el stock atómicamente
    if payload.estado in ["cancelada", "vencida"] and reserva.estado not in ["cancelada", "vencida"]:
        for d in reserva.detalles:
            stock = (
                db.query(StockInventario)
                .filter(StockInventario.id == d.stock_inventario_id)
                .with_for_update(of=StockInventario)
                .first()
            )
            if stock:
                stock.cantidad += d.cantidad

    reserva.estado = payload.estado
    db.commit()
    db.refresh(reserva)

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Actualizó el estado de la reserva #{reserva.id} a '{payload.estado}'",
        module="reservas",
    )

    return _serialize_reserva(reserva)


@router.delete("/{reserva_id}", summary="Eliminar o cancelar reserva")
async def delete_reserva(
    reserva_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Elimina o cancela una reserva y restituye el stock si estaba pendiente."""
    reserva_query = db.query(Reserva).filter(Reserva.id == reserva_id)
    reserva = scope_by_sucursal(
        reserva_query, db, current_user, Reserva.sucursal_id
    ).first()
    if not reserva:
        raise NotFoundException(f"Reserva con ID {reserva_id} no encontrada.")

    # Validar permisos: permiso administrativo o propietario de la reserva
    if current_user.role == "cliente":
        cliente = db.query(Cliente).filter(Cliente.user_id == current_user.id).first()
        if not cliente or reserva.codigo_cliente != cliente.codigo:
            raise BadRequestException("No tienes permiso para eliminar esta reserva.")
    elif current_user.role not in ["administrador", "encargado"]:
        raise BadRequestException("Permisos insuficientes.")

    if reserva.estado == "pendiente":
        for d in reserva.detalles:
            stock = db.query(StockInventario).filter(StockInventario.id == d.stock_inventario_id).first()
            if stock:
                stock.cantidad += d.cantidad

    db.delete(reserva)
    db.commit()

    BitacoraService.registrar(
        db=db,
        user=current_user,
        action=f"Eliminó la reserva #{reserva_id}",
        module="reservas",
    )

    return {"message": f"Reserva #{reserva_id} eliminada exitosamente."}
