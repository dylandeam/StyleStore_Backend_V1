"""
Servicio de Notificaciones y Suscripciones (v6 Punto 5).
Gestiona notificaciones in-app y envío por correo electrónico.
"""
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.models.notificacion import Notificacion, SuscripcionProximamente, SuscripcionStock
from app.models.user import User
from app.models.proximamente import Proximamente
from app.models.stock_inventario import StockInventario
from app.core.email import send_notification_email
from app.services.push_notification_service import PushNotificationService


class NotificacionService:
    def __init__(self, db: Session):
        self.db = db

    def crear_notificacion(
        self,
        user_id: int,
        tipo: str,
        titulo: str,
        mensaje: str,
        url_accion: Optional[str] = None,
        enviar_email: bool = True,
        enviar_push: bool = True,
        empresa_id: int | None = None,
    ) -> Notificacion:
        """Crea una notificación in-app y opcionalmente envía correo electrónico al usuario."""
        user = self.db.query(User).filter(User.id == user_id).first()
        target_empresa_id = empresa_id if empresa_id is not None else (user.empresa_id if user else None)
        if user is None or target_empresa_id is None:
            raise ValueError("No se puede notificar a un usuario sin tenant activo.")
        notif = Notificacion(
            empresa_id=target_empresa_id,
            user_id=user_id,
            tipo=tipo,
            titulo=titulo,
            mensaje=mensaje,
            url_accion=url_accion,
            leida=False,
        )
        self.db.add(notif)
        self.db.commit()
        self.db.refresh(notif)

        if enviar_email:
            if user and user.email:
                send_notification_email(
                    to_email=user.email,
                    subject=f"StyleStore: {titulo}",
                    title=titulo,
                    message=mensaje,
                    action_url=url_accion,
                )

        if enviar_push:
            PushNotificationService.send_to_user(
                self.db,
                user_id=user_id,
                empresa_id=notif.empresa_id,
                title=titulo,
                body=mensaje,
                data={
                    "notification_id": str(notif.id),
                    "type": tipo,
                    "route": url_accion or "/dashboard",
                    "empresa_id": str(notif.empresa_id),
                },
            )

        return notif

    def notificar_resultado_operacion_offline(self, operation, user: User) -> Notificacion | None:
        """Notifica una sola vez el resultado definitivo de una operación local."""
        if not operation or operation.status not in {"confirmada", "rechazada", "conflicto"}:
            return None
        action_url = f"/cuenta/operaciones-pendientes?operation_uuid={operation.operation_uuid}"
        existing = self.db.query(Notificacion).filter(
            Notificacion.empresa_id == operation.empresa_id,
            Notificacion.user_id == user.id,
            Notificacion.tipo == "resultado_operacion_offline",
            Notificacion.url_accion == action_url,
        ).first()
        if existing:
            return existing

        names = {
            "reserva": ("Reserva confirmada", "Tu reserva guardada sin conexión fue confirmada correctamente."),
            "venta_efectivo": ("Venta sincronizada", "La venta registrada sin conexión fue confirmada correctamente."),
            "resena_producto": ("Opinión publicada", "Tu comentario y calificación ya están publicados."),
        }
        if operation.status == "confirmada":
            title, message = names.get(
                operation.operation_type,
                ("Operación completada", "La acción que realizaste sin conexión fue confirmada correctamente."),
            )
        else:
            reason = str(operation.reason or "").strip()
            stock_conflict = any(word in reason.lower() for word in ("stock", "agot", "disponib"))
            if operation.operation_type == "reserva" and stock_conflict:
                title = "Reserva no realizada · prenda agotada"
                message = (
                    "Al recuperar la conexión verificamos tu reserva, pero otra compra o reserva agotó "
                    "la prenda antes de que pudiéramos confirmarla. No se descontó ni se cobró nada."
                )
            elif operation.operation_type == "reserva":
                title = "Reserva no realizada"
                message = (
                    f"No pudimos confirmar la reserva guardada sin conexión: {reason}"
                    if reason else
                    "No pudimos confirmar la reserva guardada sin conexión. Revisa el detalle e inténtalo nuevamente."
                )
            elif operation.operation_type == "venta_efectivo" and stock_conflict:
                title = "Venta offline no confirmada · sin stock"
                message = (
                    f"La venta guardada sin conexión no pudo consolidarse porque la prenda se agotó. {reason}"
                )
            else:
                title = "Operación pendiente de revisión"
                message = (
                    f"No pudimos completar la acción guardada sin conexión: {reason}"
                    if reason else
                    "No pudimos completar la acción guardada sin conexión. Revisa el detalle e inténtalo nuevamente."
                )
        return self.crear_notificacion(
            user_id=user.id,
            tipo="resultado_operacion_offline",
            titulo=title,
            mensaje=message,
            url_accion=action_url,
            enviar_email=False,
            enviar_push=True,
            empresa_id=operation.empresa_id,
        )

    def notificar_incidente_operacion_offline(self, operation, refund=None) -> int:
        """Avisa a los administradores tenant sobre conflictos/rechazos del CU44."""
        if not operation or operation.status not in {"conflicto", "rechazada"}:
            return 0
        action_url = f"/admin/procesos-offline?operation_uuid={operation.operation_uuid}"
        operation_labels = {
            "reserva": "reserva",
            "venta_efectivo": "venta en efectivo",
            "resena_producto": "reseña de producto",
        }
        label = operation_labels.get(operation.operation_type, "operación")
        reason = str(operation.reason or "No se pudo confirmar la operación.").strip()
        if refund:
            message = (
                f"La {label} offline entró en conflicto: {reason} "
                f"Se generó el reembolso #{refund.id} por {refund.moneda} {float(refund.monto):.2f} "
                f"con estado {refund.estado}."
            )
        else:
            message = f"La {label} offline entró en estado {operation.status}: {reason}"

        admins = self.db.query(User).filter(
            User.empresa_id == operation.empresa_id,
            User.role == "administrador",
            User.is_platform_admin == False,
            User.is_active == True,
        ).all()
        sent = 0
        for admin in admins:
            exists = self.db.query(Notificacion.id).filter(
                Notificacion.empresa_id == operation.empresa_id,
                Notificacion.user_id == admin.id,
                Notificacion.tipo == "incidente_operacion_offline",
                Notificacion.url_accion == action_url,
            ).first()
            if exists:
                continue
            self.crear_notificacion(
                user_id=admin.id,
                tipo="incidente_operacion_offline",
                titulo="Incidente en sincronización offline",
                mensaje=message,
                url_accion=action_url,
                enviar_email=False,
                empresa_id=operation.empresa_id,
            )
            sent += 1
        return sent

    def notificar_experiencia_poscompra(self, orden) -> Notificacion | None:
        """Envía una sola invitación a calificar por orden completada/pagada."""
        if not orden or not orden.cliente or not orden.cliente.user_id:
            return None
        user_id = orden.cliente.user_id
        empresa_id = orden.sucursal.empresa_id if orden.sucursal else None
        if empresa_id is None:
            return None
        action_url = f"/cuenta/experiencia?orden_id={orden.id}"
        existing = self.db.query(Notificacion).filter(
            Notificacion.empresa_id == empresa_id,
            Notificacion.user_id == user_id,
            Notificacion.tipo == "experiencia_poscompra",
            Notificacion.url_accion == action_url,
        ).first()
        if existing:
            return existing
        return self.crear_notificacion(
            user_id=user_id,
            tipo="experiencia_poscompra",
            titulo="¡Gracias por tu compra!",
            mensaje="Tu compra fue completada. Cuéntanos cómo fue tu experiencia o registra un reclamo si necesitas ayuda.",
            url_accion=action_url,
            enviar_email=True,
            empresa_id=empresa_id,
        )

    def notificar_nueva_venta_online(self, orden) -> int:
        """Avisa una sola vez al personal activo del tenant sobre una orden online nueva."""
        tipo_venta = str(getattr(orden, "tipo_venta", "") or "").strip().lower().replace("_", " ")
        sucursal = getattr(orden, "sucursal", None)
        empresa_id = getattr(sucursal, "empresa_id", None)
        if not orden or tipo_venta != "en linea" or empresa_id is None:
            return 0

        action_url = f"/admin/ventas?orden_id={orden.id}"
        cliente = getattr(orden, "cliente", None)
        cliente_nombre = (
            getattr(orden, "comprador_nombre", None)
            or getattr(cliente, "nombre", None)
            or getattr(orden, "codigo_cliente", None)
            or "Cliente"
        )
        sucursal_nombre = getattr(sucursal, "name", None) or "la sucursal asignada"
        mensaje = (
            f"Se registró la orden online #{orden.id} de {cliente_nombre} por "
            f"Bs {float(orden.total):.2f} para {sucursal_nombre}. Revisa su preparación y entrega."
        )

        usuarios = self.db.query(User).filter(
            User.empresa_id == empresa_id,
            User.is_active == True,
            User.is_platform_admin == False,
        ).all()
        enviados = 0
        for user in usuarios:
            role = str(user.role or "").strip().lower()
            if role in {"cliente", "customer"}:
                continue
            existente = self.db.query(Notificacion).filter(
                Notificacion.empresa_id == empresa_id,
                Notificacion.user_id == user.id,
                Notificacion.tipo == "nueva_venta_online",
                Notificacion.url_accion == action_url,
            ).first()
            if existente:
                continue
            self.crear_notificacion(
                user_id=user.id,
                tipo="nueva_venta_online",
                titulo="Nueva venta online",
                mensaje=mensaje,
                url_accion=action_url,
                enviar_email=False,
                empresa_id=empresa_id,
            )
            enviados += 1
        return enviados

    def notificar_nueva_reserva(self, reserva) -> int:
        """Avisa al personal del tenant cuando una reserva requiere preparación en sucursal."""
        sucursal = getattr(reserva, "sucursal", None)
        empresa_id = getattr(sucursal, "empresa_id", None)
        if not reserva or empresa_id is None:
            return 0
        action_url = f"/admin/reservas?reserva_id={reserva.id}"
        cliente = getattr(reserva, "cliente", None)
        cliente_nombre = getattr(cliente, "nombre", None) or getattr(reserva, "codigo_cliente", None) or "Cliente"
        mensaje = (
            f"Nueva reserva #{reserva.id} de {cliente_nombre} para {sucursal.name}. "
            "Revisa las prendas y el horario de retiro."
        )
        users = self.db.query(User).filter(
            User.empresa_id == empresa_id,
            User.is_active == True,
            User.is_platform_admin == False,
        ).all()
        sent = 0
        for user in users:
            if str(user.role or "").strip().lower() in {"cliente", "customer", "repartidor"}:
                continue
            exists = self.db.query(Notificacion).filter(
                Notificacion.empresa_id == empresa_id,
                Notificacion.user_id == user.id,
                Notificacion.tipo == "nueva_reserva",
                Notificacion.url_accion == action_url,
            ).first()
            if exists:
                continue
            self.crear_notificacion(
                user_id=user.id,
                tipo="nueva_reserva",
                titulo="Nueva reserva para preparar",
                mensaje=mensaje,
                url_accion=action_url,
                enviar_email=False,
                empresa_id=empresa_id,
            )
            sent += 1
        return sent

    def listar_notificaciones_usuario(
        self, user_id: int, empresa_id: int, limit: int = 30, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Lista las notificaciones de un usuario ordenadas de más recientes a más antiguas."""
        notifs = (
            self.db.query(Notificacion)
            .filter(Notificacion.user_id == user_id, Notificacion.empresa_id == empresa_id)
            .order_by(desc(Notificacion.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [
            {
                "id": n.id,
                "tipo": n.tipo,
                "titulo": n.titulo,
                "mensaje": n.mensaje,
                "leida": n.leida,
                "url_accion": n.url_accion,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notifs
        ]

    def contar_no_leidas(self, user_id: int, empresa_id: int) -> int:
        """Cuenta las notificaciones no leídas de un usuario."""
        return (
            self.db.query(Notificacion)
            .filter(
                Notificacion.user_id == user_id,
                Notificacion.empresa_id == empresa_id,
                Notificacion.leida == False,
            )
            .count()
        )

    def marcar_como_leida(self, notificacion_id: int, user_id: int, empresa_id: int) -> bool:
        """Marca una notificación específica como leída."""
        notif = (
            self.db.query(Notificacion)
            .filter(
                Notificacion.id == notificacion_id,
                Notificacion.user_id == user_id,
                Notificacion.empresa_id == empresa_id,
            )
            .first()
        )
        if not notif:
            return False
        notif.leida = True
        self.db.commit()
        return True

    def marcar_todas_leidas(self, user_id: int, empresa_id: int) -> int:
        """Marca todas las notificaciones no leídas de un usuario como leídas."""
        updated = (
            self.db.query(Notificacion)
            .filter(
                Notificacion.user_id == user_id,
                Notificacion.empresa_id == empresa_id,
                Notificacion.leida == False,
            )
            .update({"leida": True})
        )
        self.db.commit()
        return updated

    # ==========================================
    # Suscripciones Próximamente y Stock
    # ==========================================

    def suscribir_proximamente(self, user_id: int, proximamente_id: int) -> Dict[str, Any]:
        """Suscribe a un usuario para recibir aviso de un artículo de Próximamente."""
        existente = (
            self.db.query(SuscripcionProximamente)
            .filter(
                SuscripcionProximamente.user_id == user_id,
                SuscripcionProximamente.proximamente_id == proximamente_id,
            )
            .first()
        )
        if existente:
            return {"status": "ya_suscrito", "mensaje": "Ya estás suscrito a este artículo."}

        sub = SuscripcionProximamente(user_id=user_id, proximamente_id=proximamente_id)
        self.db.add(sub)
        self.db.commit()
        return {"status": "suscrito", "mensaje": "Te notificaremos por correo e in-app en cuanto esté disponible."}

    def suscribir_stock(self, user_id: int, stock_inventario_id: int) -> Dict[str, Any]:
        """Suscribe a un usuario para recibir aviso cuando se reponga el stock de una prenda."""
        existente = (
            self.db.query(SuscripcionStock)
            .filter(
                SuscripcionStock.user_id == user_id,
                SuscripcionStock.stock_inventario_id == stock_inventario_id,
            )
            .first()
        )
        if existente:
            return {"status": "ya_suscrito", "mensaje": "Ya estás suscrito para alertas de stock de este producto."}

        sub = SuscripcionStock(user_id=user_id, stock_inventario_id=stock_inventario_id)
        self.db.add(sub)
        self.db.commit()
        return {"status": "suscrito", "mensaje": "Te avisaremos de inmediato cuando se reponga stock."}

    def notificar_llegada_proximamente(self, proximamente_id: int, producto_codigo: Optional[str] = None) -> int:
        """Dispara notificaciones masivas a los clientes suscritos a un artículo que ya llegó."""
        subs = (
            self.db.query(SuscripcionProximamente)
            .filter(SuscripcionProximamente.proximamente_id == proximamente_id)
            .all()
        )
        item = self.db.query(Proximamente).filter(Proximamente.id == proximamente_id).first()
        nombre_item = item.nombre if (item and item.nombre) else "El artículo que esperabas"
        url = f"/catalogo/producto/{producto_codigo}" if producto_codigo else "/catalogo"
        count = len(subs)

        for s in subs:
            self.crear_notificacion(
                user_id=s.user_id,
                tipo="proximamente",
                titulo="¡Ya disponible en StyleStore!",
                mensaje=f"Buenas noticias: '{nombre_item}' ya se encuentra disponible en tienda y catálogo. ¡No te quedes sin el tuyo!",
                url_accion=url,
                enviar_email=True,
                empresa_id=item.empresa_id if item else None,
            )
            self.db.delete(s)
        self.db.commit()
        return count

    def notificar_reposicion_stock(self, stock_inventario_id: int):
        """Dispara notificaciones a los clientes suscritos a un stock que se acaba de reponer."""
        subs = (
            self.db.query(SuscripcionStock)
            .filter(SuscripcionStock.stock_inventario_id == stock_inventario_id)
            .all()
        )
        stock = self.db.query(StockInventario).filter(StockInventario.id == stock_inventario_id).first()
        prod_nom = "Prenda"
        codigo = ""
        if stock and stock.producto_color and stock.producto_color.producto:
            prod_nom = stock.producto_color.producto.nombre
            codigo = stock.producto_color.producto.codigo

        for s in subs:
            self.crear_notificacion(
                user_id=s.user_id,
                tipo="stock_disponible",
                titulo="¡Stock disponible en StyleStore!",
                mensaje=f"Tenemos stock disponible para '{prod_nom}'. Ya puedes adquirirlo o apartarlo en sucursal.",
                url_accion=f"/catalogo/producto/{codigo}" if codigo else "/catalogo",
                enviar_email=True,
                empresa_id=stock.sucursal.empresa_id if stock and stock.sucursal else None,
            )
            self.db.delete(s)
        self.db.commit()
