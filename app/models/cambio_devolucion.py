"""
Modelo CambioDevolucion (v6 Punto 9).
Conforme a Especificación StyleStore v6.
"""
from datetime import datetime, date, time
from decimal import Decimal
from sqlalchemy import Boolean, String, Integer, Date, Time, DateTime, ForeignKey, Numeric, UniqueConstraint, func, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CambioDevolucion(Base):
    """Tabla de solicitudes y registros de cambio o devolución de prendas por clientes."""

    __tablename__ = "cambios_devolucion"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    orden_venta_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orden_venta.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detalle_venta_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("detalle_venta.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipo: Mapped[str] = mapped_column(
        String(20), nullable=False, default="cambio"
    )  # 'cambio' | 'devolucion'
    motivo: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 'talla_incorrecta', 'color_incorrecto', 'producto_danado', 'disconformidad', 'otro'

    producto_nuevo_codigo: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("productos.codigo", ondelete="SET NULL"), nullable=True
    )
    talla_nueva_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tallas.id", ondelete="SET NULL"), nullable=True
    )
    color_nuevo_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("colores.id", ondelete="SET NULL"), nullable=True
    )

    sucursal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sucursales.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fecha_programada: Mapped[date] = mapped_column(Date, nullable=False)
    hora_programada: Mapped[time | None] = mapped_column(Time, nullable=True)

    descripcion_problema: Mapped[str] = mapped_column(Text, nullable=False)
    estado: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pendiente"
    )  # 'pendiente', 'aceptada', 'rechazada', 'completada'
    respuesta_encargado: Mapped[str | None] = mapped_column(Text, nullable=True)
    nuevo_stock_inventario_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("stock_inventario.id", ondelete="SET NULL"), nullable=True)
    precio_original: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    precio_nuevo: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    monto_adicional: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0", nullable=False)
    saldo_favor_generado: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0", nullable=False)
    monto_reembolsado: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0", nullable=False)
    metodo_pago_diferencia: Mapped[str | None] = mapped_column(String(30), nullable=True)
    nota_venta_numero: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True, index=True)
    orden_ajuste_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orden_venta.id", ondelete="SET NULL"), nullable=True)
    reponer_prenda_original: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relaciones
    orden_venta = relationship("OrdenVenta", lazy="joined", foreign_keys=[orden_venta_id], back_populates="cambios_solicitados")
    detalle_venta = relationship("DetalleVenta", lazy="joined")
    sucursal = relationship("Sucursal", lazy="joined")
    talla_nueva = relationship("Talla", lazy="joined", foreign_keys=[talla_nueva_id])
    color_nuevo = relationship("Color", lazy="joined", foreign_keys=[color_nuevo_id])
    producto_nuevo = relationship("Producto", lazy="joined", foreign_keys=[producto_nuevo_codigo])
    nuevo_stock = relationship("StockInventario", lazy="joined", foreign_keys=[nuevo_stock_inventario_id])
    orden_ajuste = relationship("OrdenVenta", lazy="joined", foreign_keys=[orden_ajuste_id], back_populates="cambio_ajuste")

    def __repr__(self) -> str:
        return f"<CambioDevolucion(id={self.id}, orden={self.orden_venta_id}, tipo='{self.tipo}', estado='{self.estado}')>"


class SaldoFavorCliente(Base):
    __tablename__ = "saldos_favor_cliente"
    __table_args__ = (UniqueConstraint("empresa_id", "user_id", name="uq_saldo_favor_empresa_usuario"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(Integer, ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    saldo: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MovimientoSaldoFavor(Base):
    __tablename__ = "movimientos_saldo_favor"

    id: Mapped[int] = mapped_column(primary_key=True)
    saldo_favor_id: Mapped[int] = mapped_column(Integer, ForeignKey("saldos_favor_cliente.id", ondelete="CASCADE"), nullable=False, index=True)
    cambio_devolucion_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("cambios_devolucion.id", ondelete="SET NULL"), nullable=True)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    saldo_resultante: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    descripcion: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
