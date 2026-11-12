"""
Servicio de Cambios y Devoluciones de Prendas (v6 Punto 9).
Regla: Solicitud permitida únicamente dentro de los 7 días posteriores a la fecha de compra.
"""
from datetime import date, datetime, timedelta, time
from decimal import Decimal
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session, joinedload
from fastapi import HTTPException

from app.models.cambio_devolucion import CambioDevolucion, SaldoFavorCliente, MovimientoSaldoFavor
from app.models.orden_venta import OrdenVenta, DetalleVenta
from app.models.pago import Pago
from app.models.empresa import Empresa
from app.models.stock_inventario import StockInventario
from app.models.producto_color import ProductoColor
from app.models.cliente import Cliente
from app.models.user import User
from app.services.notificacion_service import NotificacionService
from app.services.tenant_scope import get_scoped_sucursal, scope_by_sucursal


class CambioService:
    def __init__(self, db: Session, current_user: User):
        self.db = db
        self.current_user = current_user

    def _scope_branch(self, query, column):
        return scope_by_sucursal(query, self.db, self.current_user, column)

    def solicitar_cambio_devolucion(
        self,
        user: User,
        orden_venta_id: int,
        detalle_venta_id: int,
        tipo: str,
        motivo: str,
        sucursal_id: int,
        fecha_programada: date,
        descripcion_problema: str,
        hora_programada: Optional[time] = None,
        producto_nuevo_codigo: Optional[str] = None,
        talla_nueva_id: Optional[int] = None,
        color_nuevo_id: Optional[int] = None,
    ) -> CambioDevolucion:
        """Registra una solicitud de cambio o devolución validando el límite estricto de 7 días."""
        # 1. Validar cliente
        cliente = self.db.query(Cliente).filter(Cliente.user_id == user.id).first()
        if not cliente:
            raise HTTPException(status_code=400, detail="El usuario no tiene un perfil de cliente.")

        # 2. Validar que la orden pertenezca al cliente
        orden = self.db.query(OrdenVenta).filter(
            OrdenVenta.id == orden_venta_id, OrdenVenta.codigo_cliente == cliente.codigo
        ).first()
        if not orden:
            raise HTTPException(status_code=404, detail="Orden de venta no encontrada o no pertenece a este usuario.")
        if orden.sucursal_id:
            get_scoped_sucursal(self.db, user, orden.sucursal_id)
        get_scoped_sucursal(self.db, user, sucursal_id)

        # 3. Validar el plazo configurable del tenant
        fecha_compra = orden.fecha
        if isinstance(fecha_compra, datetime):
            fecha_compra = fecha_compra.date()
        elif isinstance(fecha_compra, str):
            try:
                fecha_compra = date.fromisoformat(fecha_compra[:10])
            except Exception:
                fecha_compra = date.today()
        elif fecha_compra is None:
            fecha_compra = date.today()

        empresa = self.db.query(Empresa).filter(Empresa.id == user.empresa_id).first()
        plazo_dias = empresa.plazo_cambios_dias if empresa else 7
        limite = fecha_compra + timedelta(days=plazo_dias)
        if date.today() > limite:
            raise HTTPException(
                status_code=400,
                detail=f"El plazo de garantía para cambios o devoluciones es de {plazo_dias} días posteriores a la compra. Tu compra fue el {fecha_compra.strftime('%d/%m/%Y')} y expiró el {limite.strftime('%d/%m/%Y')}."
            )

        # 4. Validar detalle de venta
        detalle = self.db.query(DetalleVenta).filter(
            DetalleVenta.id == detalle_venta_id, DetalleVenta.orden_venta_id == orden.id
        ).first()
        if not detalle:
            raise HTTPException(status_code=404, detail="El artículo no pertenece a la orden indicada.")

        # La selección del cliente es una preferencia. La existencia definitiva la
        # confirma caja con stock real al preparar la liquidación.
        if tipo == "devolucion":
            producto_nuevo_codigo = None
            talla_nueva_id = None
            color_nuevo_id = None
        elif producto_nuevo_codigo:
            preferencia = self._scope_branch(
                self.db.query(StockInventario).join(
                    ProductoColor, StockInventario.producto_color_id == ProductoColor.id
                ),
                StockInventario.sucursal_id,
            ).filter(
                StockInventario.sucursal_id == sucursal_id,
                StockInventario.cantidad > 0,
                ProductoColor.producto_codigo == producto_nuevo_codigo,
            )
            if talla_nueva_id:
                preferencia = preferencia.filter(StockInventario.talla_id == talla_nueva_id)
            if color_nuevo_id:
                preferencia = preferencia.filter(ProductoColor.color_id == color_nuevo_id)
            if not preferencia.first():
                raise HTTPException(
                    status_code=400,
                    detail="La prenda preferida ya no tiene una variante disponible en esta sucursal. Elige otra opción.",
                )

        # 5. Crear la solicitud
        solicitud = CambioDevolucion(
            orden_venta_id=orden.id,
            detalle_venta_id=detalle.id,
            tipo=tipo,
            motivo=motivo,
            sucursal_id=sucursal_id,
            fecha_programada=fecha_programada,
            hora_programada=hora_programada,
            producto_nuevo_codigo=producto_nuevo_codigo,
            talla_nueva_id=talla_nueva_id,
            color_nuevo_id=color_nuevo_id,
            descripcion_problema=descripcion_problema,
            estado="pendiente",
        )
        self.db.add(solicitud)
        self.db.commit()
        self.db.refresh(solicitud)

        # Notificar in-app al cliente de su solicitud recibida (sin bloquear la petición HTTP)
        notif_srv = NotificacionService(self.db)
        notif_srv.crear_notificacion(
            user_id=user.id,
            tipo="cambio_devolucion",
            titulo="Solicitud de Cambio/Devolución Recibida",
            mensaje=f"Hemos recibido tu solicitud de {tipo} para el producto de tu orden {orden.ticket_numero or orden.id}. Un encargado revisará tu caso.",
            url_accion="/cuenta/mis-compras",
            enviar_email=False,
            empresa_id=orden.sucursal.empresa_id,
        )

        return solicitud

    def listar_solicitudes(
        self,
        sucursal_id: Optional[int] = None,
        estado: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[CambioDevolucion]:
        """Lista solicitudes para el panel de administración / encargado."""
        # Repara de forma idempotente liquidaciones que hayan sido cobradas por
        # el endpoint genérico de caja antes de que existiera el cierre unificado.
        # Así el estado, inventario y confirmación del cliente nunca divergen.
        self.conciliar_liquidaciones_pagadas()
        q = self.db.query(CambioDevolucion).options(
            joinedload(CambioDevolucion.orden_venta).joinedload(OrdenVenta.cliente).joinedload(Cliente.user),
            joinedload(CambioDevolucion.detalle_venta),
            joinedload(CambioDevolucion.sucursal),
            joinedload(CambioDevolucion.producto_nuevo),
            joinedload(CambioDevolucion.talla_nueva),
            joinedload(CambioDevolucion.color_nuevo),
        )
        q = self._scope_branch(q, CambioDevolucion.sucursal_id)
        if sucursal_id:
            q = q.filter(CambioDevolucion.sucursal_id == sucursal_id)
        if estado:
            q = q.filter(CambioDevolucion.estado == estado)

        return q.order_by(CambioDevolucion.id.desc()).offset(offset).limit(limit).all()

    def conciliar_liquidaciones_pagadas(self) -> int:
        """Completa ajustes pagados que todavía figuran pendientes de liquidación."""
        pendientes = self._scope_branch(
            self.db.query(CambioDevolucion).join(
                OrdenVenta, CambioDevolucion.orden_ajuste_id == OrdenVenta.id
            ),
            CambioDevolucion.sucursal_id,
        ).filter(
            CambioDevolucion.estado == "pendiente_liquidacion",
            OrdenVenta.estado == "pagada",
        ).all()

        completadas = 0
        for solicitud in pendientes:
            pago = (
                self.db.query(Pago)
                .filter(Pago.orden_venta_id == solicitud.orden_ajuste_id)
                .order_by(Pago.id.desc())
                .first()
            )
            metodo = (
                (pago.metodo_pago if pago else None)
                or (solicitud.orden_ajuste.metodo_pago if solicitud.orden_ajuste else None)
                or "efectivo"
            ).lower()
            if metodo not in {"efectivo", "qr", "transferencia"}:
                metodo = "efectivo"
            metodo_cobro = metodo if metodo in {"efectivo", "qr"} else "efectivo"
            self.completar_en_caja(
                solicitud.id,
                metodo_pago_diferencia=metodo_cobro if Decimal(solicitud.monto_adicional or 0) > 0 else None,
                efectivo_recibido=Decimal(solicitud.monto_adicional or 0),
                metodo_reembolso=metodo if Decimal(solicitud.monto_reembolsado or 0) > 0 else None,
            )
            completadas += 1
        return completadas

    def listar_mis_solicitudes(self, user: User) -> List[CambioDevolucion]:
        """Lista las solicitudes del cliente autenticado."""
        cliente = self.db.query(Cliente).filter(Cliente.user_id == user.id).first()
        if not cliente:
            return []

        return (
            self.db.query(CambioDevolucion)
            .join(OrdenVenta, CambioDevolucion.orden_venta_id == OrdenVenta.id)
            .filter(OrdenVenta.codigo_cliente == cliente.codigo)
            .options(
                joinedload(CambioDevolucion.orden_venta),
                joinedload(CambioDevolucion.detalle_venta),
                joinedload(CambioDevolucion.sucursal),
                joinedload(CambioDevolucion.producto_nuevo),
                joinedload(CambioDevolucion.talla_nueva),
                joinedload(CambioDevolucion.color_nuevo),
            )
            .order_by(CambioDevolucion.id.desc())
            .all()
        )

    def responder_solicitud(
        self,
        solicitud_id: int,
        nuevo_estado: str,  # 'aceptada' | 'rechazada'
        respuesta_encargado: str,
    ) -> CambioDevolucion:
        """El encargado o administrador aprueba o rechaza la solicitud y notifica al cliente."""
        solicitud_query = self.db.query(CambioDevolucion).options(
            joinedload(CambioDevolucion.orden_venta).joinedload(OrdenVenta.cliente).joinedload(Cliente.user)
        )
        solicitud = self._scope_branch(
            solicitud_query, CambioDevolucion.sucursal_id
        ).filter(CambioDevolucion.id == solicitud_id).first()
        if not solicitud:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada.")

        solicitud.estado = nuevo_estado
        solicitud.respuesta_encargado = respuesta_encargado
        self.db.commit()

        # Notificar al cliente
        if solicitud.orden_venta and solicitud.orden_venta.cliente and solicitud.orden_venta.cliente.user:
            user = solicitud.orden_venta.cliente.user
            titulo = f"Solicitud de {solicitud.tipo.title()} {nuevo_estado.upper()}"
            mensaje = f"Tu solicitud para la orden {solicitud.orden_venta.ticket_numero or solicitud.orden_venta_id} ha sido {nuevo_estado}. Mensaje del encargado: {respuesta_encargado}"
            notif_srv = NotificacionService(self.db)
            notif_srv.crear_notificacion(
                user_id=user.id,
                tipo="cambio_devolucion",
                titulo=titulo,
                mensaje=mensaje,
                url_accion="/cuenta/mis-compras",
                enviar_email=True,
                empresa_id=solicitud.sucursal.empresa_id,
            )

        return solicitud

    def _completar_en_caja_legacy(
        self,
        solicitud_id: int,
        reponer_prenda_original: bool = True,
        nuevo_stock_inventario_id: Optional[int] = None,
        metodo_pago_diferencia: Optional[str] = None,
        efectivo_recibido: Optional[Decimal] = None,
        metodo_reembolso: Optional[str] = None,
    ) -> CambioDevolucion:
        """Liquida el cambio de forma atómica, emite nota y ajusta inventario/saldo."""
        solicitud_query = self.db.query(CambioDevolucion).options(
            joinedload(CambioDevolucion.detalle_venta),
            joinedload(CambioDevolucion.orden_venta),
        )
        solicitud = self._scope_branch(
            solicitud_query, CambioDevolucion.sucursal_id
        ).filter(CambioDevolucion.id == solicitud_id).first()
        if not solicitud:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada.")
        if solicitud.estado != "aceptada":
            raise HTTPException(status_code=400, detail="Solo se puede completar una solicitud aceptada.")

        detalle = solicitud.detalle_venta
        if not detalle:
            raise HTTPException(status_code=400, detail="La solicitud no tiene una prenda original válida.")
        precio_original = Decimal(detalle.precio_unitario)
        precio_nuevo = Decimal("0.00")
        nuevo_stk = None

        if solicitud.tipo == "cambio":
            if not nuevo_stock_inventario_id:
                # Compatibilidad con solicitudes anteriores: canje por la misma variante.
                nuevo_stock_inventario_id = detalle.stock_inventario_id
            if not nuevo_stock_inventario_id:
                raise HTTPException(status_code=400, detail="Selecciona la nueva prenda que se entregará.")
            nuevo_stk = self._scope_branch(
                self.db.query(StockInventario), StockInventario.sucursal_id
            ).filter(
                StockInventario.id == nuevo_stock_inventario_id,
                StockInventario.sucursal_id == solicitud.sucursal_id,
            ).with_for_update(of=StockInventario).first()
            if not nuevo_stk or nuevo_stk.cantidad <= 0:
                raise HTTPException(status_code=400, detail="La nueva prenda no tiene stock disponible en la sucursal elegida.")
            producto = nuevo_stk.producto_color.producto if nuevo_stk.producto_color else None
            if not producto:
                raise HTTPException(status_code=400, detail="La nueva prenda no es válida.")
            precio_nuevo = Decimal(nuevo_stk.precio_variante if nuevo_stk.precio_variante is not None else producto.precio)

        diferencia = precio_nuevo - precio_original if solicitud.tipo == "cambio" else -precio_original
        monto_adicional = max(diferencia, Decimal("0.00"))
        saldo_generado = abs(min(diferencia, Decimal("0.00"))) if solicitud.tipo == "cambio" else Decimal("0.00")
        monto_reembolsado = precio_original if solicitud.tipo == "devolucion" else Decimal("0.00")
        metodo = (metodo_pago_diferencia or "").lower()
        if monto_adicional > 0:
            if metodo not in {"efectivo", "qr"}:
                raise HTTPException(status_code=400, detail="La diferencia debe cobrarse por efectivo o QR.")
            if metodo == "efectivo" and (efectivo_recibido is None or efectivo_recibido < monto_adicional):
                raise HTTPException(status_code=400, detail="El efectivo recibido no cubre la diferencia del cambio.")
        if solicitud.tipo == "devolucion" and (metodo_reembolso or "").lower() not in {"efectivo", "transferencia"}:
            raise HTTPException(status_code=400, detail="Indica si el reembolso se realizó en efectivo o transferencia.")

        if reponer_prenda_original and detalle.stock_inventario_id:
            stk = self._scope_branch(self.db.query(StockInventario), StockInventario.sucursal_id).filter(
                StockInventario.id == detalle.stock_inventario_id
            ).with_for_update(of=StockInventario).first()
            if stk:
                stk.cantidad += 1

        if nuevo_stk:
            nuevo_stk.cantidad -= 1

        # Fuerza ambos movimientos dentro de la misma transacción antes de
        # liquidar el comprobante. Si cualquier escritura falla, no se marca
        # la solicitud como completada ni se deja un inventario parcial.
        self.db.flush()

        cliente = solicitud.orden_venta.cliente if solicitud.orden_venta else None
        if not cliente or not cliente.user:
            raise HTTPException(status_code=400, detail="No se pudo identificar al cliente del cambio.")

        if saldo_generado > 0:
            empresa_id = solicitud.sucursal.empresa_id
            cuenta = self.db.query(SaldoFavorCliente).filter(
                SaldoFavorCliente.empresa_id == empresa_id,
                SaldoFavorCliente.user_id == cliente.user_id,
            ).with_for_update(of=SaldoFavorCliente).first()
            if not cuenta:
                cuenta = SaldoFavorCliente(empresa_id=empresa_id, user_id=cliente.user_id, saldo=Decimal("0.00"))
                self.db.add(cuenta)
                self.db.flush()
            cuenta.saldo = Decimal(cuenta.saldo) + saldo_generado
            self.db.add(MovimientoSaldoFavor(
                saldo_favor_id=cuenta.id, cambio_devolucion_id=solicitud.id,
                tipo="credito", monto=saldo_generado, saldo_resultante=cuenta.saldo,
                descripcion=f"Saldo por cambio #{solicitud.id} a una prenda de menor precio",
            ))

        now = datetime.now()
        nota = f"NCJ-{now.strftime('%Y%m%d%H%M%S')}-{solicitud.id}"
        orden_ajuste = OrdenVenta(
            fecha=now.date(), estado="pagada", total=monto_adicional, tipo_venta="cambio",
            codigo_cliente=cliente.codigo, sucursal_id=solicitud.sucursal_id,
            ticket_numero=nota,
            metodo_pago=metodo if monto_adicional > 0 else (metodo_reembolso or "saldo_favor"),
        )
        self.db.add(orden_ajuste)
        self.db.flush()
        if nuevo_stk:
            producto = nuevo_stk.producto_color.producto
            self.db.add(DetalleVenta(
                orden_venta_id=orden_ajuste.id, stock_inventario_id=nuevo_stk.id,
                producto_nombre=producto.nombre,
                color_nombre=nuevo_stk.producto_color.color.nombre if nuevo_stk.producto_color.color else None,
                talla_nombre=nuevo_stk.talla.nombre if nuevo_stk.talla else None,
                cantidad=1, precio_unitario=precio_nuevo, subtotal=precio_nuevo,
            ))
        pago_adicional = None
        if monto_adicional > 0:
            pago_adicional = Pago(
                orden_venta_id=orden_ajuste.id, monto=monto_adicional,
                empresa_id=solicitud.sucursal.empresa_id,
                tipo_pago="en caja", metodo_pago=metodo, estado="aprobado",
            )
            self.db.add(pago_adicional)

        solicitud.estado = "completada"
        solicitud.nuevo_stock_inventario_id = nuevo_stk.id if nuevo_stk else None
        solicitud.producto_nuevo_codigo = nuevo_stk.producto_color.producto_codigo if nuevo_stk else solicitud.producto_nuevo_codigo
        solicitud.talla_nueva_id = nuevo_stk.talla_id if nuevo_stk else solicitud.talla_nueva_id
        solicitud.color_nuevo_id = nuevo_stk.producto_color.color_id if nuevo_stk else solicitud.color_nuevo_id
        solicitud.precio_original = precio_original
        solicitud.precio_nuevo = precio_nuevo if nuevo_stk else None
        solicitud.monto_adicional = monto_adicional
        solicitud.saldo_favor_generado = saldo_generado
        solicitud.monto_reembolsado = monto_reembolsado
        solicitud.metodo_pago_diferencia = metodo if monto_adicional > 0 else (metodo_reembolso or None)
        solicitud.nota_venta_numero = nota
        solicitud.orden_ajuste_id = orden_ajuste.id
        self.db.commit()
        self.db.refresh(solicitud)
        if pago_adicional:
            from app.services.pago_service import PagoService
            self.db.refresh(pago_adicional)
            PagoService(self.db).confirmar(pago_adicional)
        resultado = (
            f"Debes pagar Bs {monto_adicional:.2f}." if monto_adicional > 0
            else f"Generamos Bs {saldo_generado:.2f} de saldo a favor." if saldo_generado > 0
            else f"Se registró un reembolso de Bs {monto_reembolsado:.2f}." if monto_reembolsado > 0
            else "El cambio no tuvo diferencia de precio."
        )
        NotificacionService(self.db).crear_notificacion(
            user_id=cliente.user_id,
            tipo="cambio_devolucion",
            titulo="Cambio/Devolución completado",
            mensaje=f"Operación {nota} completada. {resultado}",
            url_accion="/cuenta/mis-compras",
            enviar_email=True,
            empresa_id=solicitud.sucursal.empresa_id,
        )
        return solicitud

    def preparar_liquidacion(
        self,
        solicitud_id: int,
        reponer_prenda_original: bool = True,
        nuevo_stock_inventario_id: Optional[int] = None,
    ) -> CambioDevolucion:
        """Calcula el ajuste y lo deja pendiente para que caja lo revise y liquide."""
        solicitud = self._scope_branch(
            self.db.query(CambioDevolucion).options(
                joinedload(CambioDevolucion.detalle_venta),
                joinedload(CambioDevolucion.orden_venta).joinedload(OrdenVenta.cliente).joinedload(Cliente.user),
            ),
            CambioDevolucion.sucursal_id,
        ).filter(CambioDevolucion.id == solicitud_id).first()
        if not solicitud:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada.")
        if solicitud.estado not in {"aceptada", "pendiente_liquidacion"}:
            raise HTTPException(status_code=400, detail="Solo una solicitud aceptada puede enviarse a caja.")
        if solicitud.orden_ajuste and solicitud.orden_ajuste.estado == "pendiente_pago":
            solicitud.orden_ajuste.estado = "cancelada"
        detalle = solicitud.detalle_venta
        if not detalle:
            raise HTTPException(status_code=400, detail="La solicitud no tiene una prenda original válida.")
        cliente = solicitud.orden_venta.cliente if solicitud.orden_venta else None
        if not cliente or not cliente.user:
            raise HTTPException(status_code=400, detail="No se pudo identificar al cliente del cambio.")

        precio_original = Decimal(detalle.precio_unitario)
        precio_nuevo = Decimal("0.00")
        nuevo_stk = None
        if solicitud.tipo == "cambio":
            nuevo_stock_inventario_id = nuevo_stock_inventario_id or detalle.stock_inventario_id
            if not nuevo_stock_inventario_id:
                raise HTTPException(status_code=400, detail="Selecciona la nueva prenda que se entregará.")
            nuevo_stk = self._scope_branch(
                self.db.query(StockInventario), StockInventario.sucursal_id
            ).filter(
                StockInventario.id == nuevo_stock_inventario_id,
                StockInventario.sucursal_id == solicitud.sucursal_id,
            ).first()
            if not nuevo_stk or nuevo_stk.cantidad <= 0:
                raise HTTPException(status_code=400, detail="La nueva prenda no tiene stock disponible en esta sucursal.")
            producto = nuevo_stk.producto_color.producto if nuevo_stk.producto_color else None
            if not producto:
                raise HTTPException(status_code=400, detail="La nueva prenda no es válida.")
            precio_nuevo = Decimal(
                nuevo_stk.precio_variante if nuevo_stk.precio_variante is not None else producto.precio
            )

        diferencia = precio_nuevo - precio_original if solicitud.tipo == "cambio" else -precio_original
        monto_adicional = max(diferencia, Decimal("0.00"))
        monto_reembolsado = abs(min(diferencia, Decimal("0.00")))
        tipo_ajuste = (
            "cambio_cobro" if monto_adicional > 0
            else "cambio_reembolso" if monto_reembolsado > 0
            else "cambio_sin_ajuste"
        )
        monto_caja = monto_adicional if monto_adicional > 0 else monto_reembolsado
        now = datetime.now()
        nota = f"NCJ-{now.strftime('%Y%m%d%H%M%S')}-{solicitud.id}"
        orden_ajuste = OrdenVenta(
            fecha=now.date(),
            estado="pendiente_pago",
            total=monto_caja,
            tipo_venta=tipo_ajuste,
            codigo_cliente=cliente.codigo,
            sucursal_id=solicitud.sucursal_id,
            ticket_numero=nota,
            metodo_pago=None,
            inventario_descontado=False,
        )
        self.db.add(orden_ajuste)
        self.db.flush()
        if nuevo_stk:
            producto = nuevo_stk.producto_color.producto
            self.db.add(DetalleVenta(
                orden_venta_id=orden_ajuste.id,
                stock_inventario_id=nuevo_stk.id,
                producto_nombre=producto.nombre,
                color_nombre=nuevo_stk.producto_color.color.nombre if nuevo_stk.producto_color.color else None,
                talla_nombre=nuevo_stk.talla.nombre if nuevo_stk.talla else None,
                cantidad=1,
                precio_unitario=precio_nuevo,
                subtotal=precio_nuevo,
            ))

        solicitud.estado = "pendiente_liquidacion"
        solicitud.reponer_prenda_original = reponer_prenda_original
        solicitud.nuevo_stock_inventario_id = nuevo_stk.id if nuevo_stk else None
        solicitud.producto_nuevo_codigo = nuevo_stk.producto_color.producto_codigo if nuevo_stk else solicitud.producto_nuevo_codigo
        solicitud.talla_nueva_id = nuevo_stk.talla_id if nuevo_stk else solicitud.talla_nueva_id
        solicitud.color_nuevo_id = nuevo_stk.producto_color.color_id if nuevo_stk else solicitud.color_nuevo_id
        solicitud.precio_original = precio_original
        solicitud.precio_nuevo = precio_nuevo if nuevo_stk else None
        solicitud.monto_adicional = monto_adicional
        solicitud.saldo_favor_generado = Decimal("0.00")
        solicitud.monto_reembolsado = monto_reembolsado
        solicitud.nota_venta_numero = nota
        solicitud.orden_ajuste_id = orden_ajuste.id
        self.db.commit()
        self.db.refresh(solicitud)
        NotificacionService(self.db).crear_notificacion(
            user_id=cliente.user_id,
            tipo="cambio_devolucion",
            titulo="Ajuste preparado en caja",
            mensaje=(
                f"Tu solicitud #{solicitud.id} fue revisada. "
                + (f"Queda pendiente pagar Bs {monto_adicional:.2f} en caja."
                   if monto_adicional > 0 else
                   f"Queda pendiente devolverte Bs {monto_reembolsado:.2f} en caja."
                   if monto_reembolsado > 0 else
                   "El cambio no tiene diferencia de precio y queda pendiente de confirmación en caja.")
            ),
            url_accion="/cuenta/mis-compras",
            enviar_email=True,
            empresa_id=solicitud.sucursal.empresa_id,
        )
        return solicitud

    def completar_en_caja(
        self,
        solicitud_id: int,
        metodo_pago_diferencia: Optional[str] = None,
        efectivo_recibido: Optional[Decimal] = None,
        metodo_reembolso: Optional[str] = None,
        **_compat,
    ) -> CambioDevolucion:
        """Confirma en caja un ajuste previamente preparado y recién entonces mueve stock."""
        solicitud = self._scope_branch(
            self.db.query(CambioDevolucion).options(
                joinedload(CambioDevolucion.detalle_venta),
                joinedload(CambioDevolucion.orden_venta).joinedload(OrdenVenta.cliente).joinedload(Cliente.user),
                joinedload(CambioDevolucion.orden_ajuste),
            ),
            CambioDevolucion.sucursal_id,
        ).filter(CambioDevolucion.id == solicitud_id).first()
        if not solicitud:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada.")
        if solicitud.estado == "completada":
            return solicitud
        if solicitud.estado != "pendiente_liquidacion" or not solicitud.orden_ajuste:
            raise HTTPException(status_code=400, detail="Primero debes preparar el ajuste y enviarlo a caja.")

        monto_adicional = Decimal(solicitud.monto_adicional or 0)
        monto_reembolso = Decimal(solicitud.monto_reembolsado or 0)
        metodo_cobro = (metodo_pago_diferencia or "").lower()
        metodo_salida = (metodo_reembolso or "").lower()
        if monto_adicional > 0:
            if metodo_cobro not in {"efectivo", "qr"}:
                raise HTTPException(status_code=400, detail="La diferencia debe cobrarse por efectivo o QR.")
            if metodo_cobro == "efectivo" and (efectivo_recibido is None or efectivo_recibido < monto_adicional):
                raise HTTPException(status_code=400, detail="El efectivo recibido no cubre la diferencia del cambio.")
        if monto_reembolso > 0 and metodo_salida not in {"efectivo", "qr", "transferencia"}:
            raise HTTPException(status_code=400, detail="Indica si la devolución se realizó en efectivo, QR o transferencia.")

        detalle = solicitud.detalle_venta
        nuevo_stk = None
        if solicitud.tipo == "cambio":
            nuevo_stk = self._scope_branch(
                self.db.query(StockInventario), StockInventario.sucursal_id
            ).filter(
                StockInventario.id == solicitud.nuevo_stock_inventario_id,
                StockInventario.sucursal_id == solicitud.sucursal_id,
            ).with_for_update(of=StockInventario).first()
            if not nuevo_stk or nuevo_stk.cantidad <= 0:
                raise HTTPException(status_code=409, detail="La prenda elegida se agotó antes de liquidar el cambio. Selecciona otra existencia.")
        if solicitud.reponer_prenda_original and detalle and detalle.stock_inventario_id:
            original = self._scope_branch(
                self.db.query(StockInventario), StockInventario.sucursal_id
            ).filter(StockInventario.id == detalle.stock_inventario_id).with_for_update(of=StockInventario).first()
            if original:
                original.cantidad += 1
        if nuevo_stk:
            nuevo_stk.cantidad -= 1

        cliente = solicitud.orden_venta.cliente if solicitud.orden_venta else None
        if not cliente or not cliente.user:
            raise HTTPException(status_code=400, detail="No se pudo identificar al cliente del cambio.")
        orden_ajuste = solicitud.orden_ajuste
        pago = (
            self.db.query(Pago)
            .filter(Pago.orden_venta_id == orden_ajuste.id)
            .order_by(Pago.id.desc())
            .first()
        )
        metodo = metodo_cobro if monto_adicional > 0 else metodo_salida if monto_reembolso > 0 else "sin_ajuste"
        orden_ajuste.estado = "pagada"
        orden_ajuste.metodo_pago = metodo
        orden_ajuste.inventario_descontado = True
        if monto_adicional > 0:
            if not pago:
                pago = Pago(
                    orden_venta_id=orden_ajuste.id,
                    monto=monto_adicional,
                    empresa_id=solicitud.sucursal.empresa_id,
                )
                self.db.add(pago)
            pago.tipo_pago = "en caja"
            pago.metodo_pago = metodo
            pago.estado = "aprobado"
        elif monto_reembolso > 0:
            if not pago:
                pago = Pago(
                    orden_venta_id=orden_ajuste.id,
                    monto=monto_reembolso,
                    empresa_id=solicitud.sucursal.empresa_id,
                )
                self.db.add(pago)
            pago.tipo_pago = "devolucion en caja"
            pago.metodo_pago = metodo
            pago.estado = "reembolsado"
            pago.refund_reason = f"Liquidación de cambio/devolución #{solicitud.id}"
            pago.refunded_at = datetime.now()
            pago.recibo_numero = pago.recibo_numero or f"DEV-{orden_ajuste.id:08d}"

        solicitud.estado = "completada"
        solicitud.metodo_pago_diferencia = metodo
        self.db.commit()
        self.db.refresh(solicitud)
        if pago and monto_adicional > 0:
            from app.services.pago_service import PagoService
            self.db.refresh(pago)
            PagoService(self.db).confirmar(pago)
        resultado = (
            f"Se cobró la diferencia de Bs {monto_adicional:.2f}." if monto_adicional > 0
            else f"Se devolvieron Bs {monto_reembolso:.2f}." if monto_reembolso > 0
            else "El cambio fue confirmado sin diferencia de precio."
        )
        NotificacionService(self.db).crear_notificacion(
            user_id=cliente.user_id,
            tipo="cambio_devolucion",
            titulo="Cambio/Devolución completado",
            mensaje=f"Operación {solicitud.nota_venta_numero} completada. {resultado}",
            url_accion="/cuenta/mis-compras",
            enviar_email=True,
            empresa_id=solicitud.sucursal.empresa_id,
        )
        return solicitud
