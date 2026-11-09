"""
Pydantic schemas para Pagos, Ventas y Envíos (v5 secciones 19, 20, 21, 22, 23, 24).
"""
from datetime import date, datetime, time
from decimal import Decimal
from pydantic import BaseModel, Field, model_validator


# --- PAGOS ---
class PagoCreateRequest(BaseModel):
    orden_venta_id: int
    tipo_pago: str = Field("en linea", description="'en linea' (PayPal) o 'en caja'")
    paypal_order_id: str | None = None
    paypal_capture_id: str | None = None


class PagoResponse(BaseModel):
    id: int
    orden_venta_id: int
    empresa_id: int | None = None
    monto: Decimal
    tipo_pago: str
    metodo_pago: str | None = None
    estado: str
    paypal_order_id: str | None = None
    paypal_capture_id: str | None = None
    operation_uuid: str | None = None
    paypal_refund_id: str | None = None
    refund_reason: str | None = None
    refunded_at: datetime | None = None
    recibo_numero: str | None = None
    email_confirmacion_enviado_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PayPalCrearOrdenRequest(BaseModel):
    orden_venta_id: int
    return_url: str | None = None
    cancel_url: str | None = None


class PayPalCapturarOrdenRequest(BaseModel):
    paypal_order_id: str
    orden_venta_id: int | None = None


class PayPalReconciliationItem(BaseModel):
    orden_venta_id: int
    payment_operation_uuid: str
    estado: str
    paypal_order_id: str
    paypal_capture_id: str | None = None
    paypal_refund_id: str | None = None
    mensaje: str


class OfflineCheckoutPreview(BaseModel):
    producto: str
    color: str | None = None
    talla: str | None = None
    sucursal: str
    cantidad: int
    precio_unitario: Decimal
    total: Decimal
    moneda: str = "BOB"
    cliente: str
    estado: str = "preparado"
    orden_venta_id: int | None = None


class OfflineCheckoutStartResponse(OfflineCheckoutPreview):
    paypal_order_id: str
    approve_url: str
    paypal_total: str | None = None
    paypal_currency: str | None = None


class CobroCajaRequest(BaseModel):
    orden_venta_id: int
    efectivo_recibido: Decimal = Field(..., ge=Decimal("0"))
    metodo_pago: str | None = Field(default="efectivo", description="'efectivo' o 'qr'")


class CobroCajaResponse(BaseModel):
    pago_id: int
    orden_venta_id: int
    total: Decimal
    efectivo_recibido: Decimal
    cambio_devuelto: Decimal
    ticket_numero: str
    recibo_numero: str | None = None
    fecha: datetime


class QRConfigDTO(BaseModel):
    imagen_url: str
    banco_destino: str | None = "QR Simple - Red Enlace / Banco Unión / BCP / BNB"
    titular: str | None = "StyleStore Bolivia"
    sucursal_id: int | None = None


class QRConfigResponse(BaseModel):
    id: int | None = None
    imagen_url: str | None = None
    banco_destino: str | None = None
    titular: str | None = None
    activo: bool = True
    updated_at: str | None = None


# --- ENVÍOS ---
class EnvioCotizacionRequest(BaseModel):
    distancia_km: float = Field(..., ge=0, description="Distancia estimada en kilómetros entre sucursal y destino")


class EnvioCreateRequest(BaseModel):
    orden_venta_id: int
    direccion: str = Field(..., min_length=2, max_length=255)
    ciudad: str = Field(..., min_length=2, max_length=100)
    referencia: str | None = None
    ubicacion_url: str | None = Field(None, max_length=500, description="Enlace de Google Maps o Mapas de iOS")
    distancia_km: float | None = Field(None, ge=0)
    costo: Decimal | None = None
    latitud_destino: float | None = None
    longitud_destino: float | None = None


class EnvioUpdateRequest(BaseModel):
    direccion: str | None = None
    ciudad: str | None = None
    referencia: str | None = None
    ubicacion_url: str | None = None
    costo: Decimal | None = None
    estado: str | None = None
    latitud_destino: float | None = None
    longitud_destino: float | None = None
    yango_tracking_code: str | None = None
    yango_tracking_url: str | None = None
    tracking_code: str | None = None
    tracking_url: str | None = None
    delivery_conductor: str | None = None
    tracking_activo: bool | None = None


class EnvioDeliveryUpdateRequest(BaseModel):
    tracking_code: str | None = None
    tracking_url: str | None = None
    yango_tracking_code: str | None = None
    yango_tracking_url: str | None = None
    delivery_conductor: str | None = None
    estado: str | None = None


# Alias de compatibilidad hacia atrás
EnvioYangoUpdateRequest = EnvioDeliveryUpdateRequest


class EnvioResponse(BaseModel):
    id: int
    orden_venta_id: int
    direccion: str
    ciudad: str
    referencia: str | None = None
    ubicacion_url: str | None = None
    costo: Decimal
    estado: str
    fecha: date
    yango_tracking_code: str | None = None
    yango_tracking_url: str | None = None
    tracking_code: str | None = None
    tracking_url: str | None = None
    delivery_conductor: str | None = None
    token_seguimiento: str | None = None
    tracking_activo: bool = True
    distancia_km: float | None = None
    minutos_estimados: int | None = None
    latitud_destino: float | None = None
    longitud_destino: float | None = None
    repartidor_id: int | None = None
    repartidor_nombre: str | None = None
    repartidor_lat: float | None = None
    repartidor_lon: float | None = None
    repartidor_actualizado_en: str | None = None
    created_at: datetime
    cliente_nombre: str | None = None

    model_config = {"from_attributes": True}


# --- VENTAS ---
class VentaItemCreate(BaseModel):
    stock_inventario_id: int
    cantidad: int = Field(1, ge=1)


class VentaPresencialCreate(BaseModel):
    operation_uuid: str | None = Field(None, min_length=36, max_length=36, exclude=True)
    codigo_cliente: str | None = Field(None, max_length=30)
    comprador_nombre: str | None = Field(None, min_length=2, max_length=160)
    comprador_ci: str | None = Field(None, min_length=4, max_length=20)
    sucursal_id: int
    items: list[VentaItemCreate] = Field(..., min_length=1)
    metodo_pago: str = Field("efectivo", description="'efectivo' o 'qr'")
    efectivo_recibido: Decimal | None = None
    codigo_descuento: str | None = Field(None, max_length=40)

    @model_validator(mode="after")
    def validar_comprador(self):
        if not self.codigo_cliente and (not self.comprador_nombre or not self.comprador_ci):
            raise ValueError("Selecciona un cliente registrado o ingresa nombre y CI para la factura.")
        return self


class DetalleVentaResponse(BaseModel):
    id: int
    producto_nombre: str
    color_nombre: str | None = None
    talla_nombre: str | None = None
    cantidad: int
    precio_unitario: Decimal
    subtotal: Decimal

    model_config = {"from_attributes": True}


class OrdenVentaResponse(BaseModel):
    id: int
    fecha: date
    estado: str
    total: Decimal
    subtotal_productos: Decimal | None = None
    descuento_promociones: Decimal = Decimal("0.00")
    descuento_codigo: Decimal = Decimal("0.00")
    codigo_descuento: str | None = None
    porcentaje_codigo: int = 0
    tipo_venta: str
    codigo_cliente: str | None = None
    comprador_nombre: str | None = None
    comprador_ci: str | None = None
    metodo_pago: str | None = None
    ticket_numero: str | None = None
    sucursal_id: int | None = None
    cliente_nombre: str | None = None
    cliente_email: str | None = None
    cliente_telefono: str | None = None
    sucursal_ciudad: str | None = None
    sucursal_nombre: str | None = None
    sucursal_direccion: str | None = None
    detalles: list[DetalleVentaResponse] = []
    envio: EnvioResponse | None = None
    created_at: datetime
    tipo_entrega: str | None = None
    fecha_recojo: date | None = None
    hora_recojo: time | None = None
    pago_id: int | None = None
    pago_estado: str | None = None
    recibo_numero: str | None = None
    pago_email_confirmado: bool = False
    payment_operation_uuid: str | None = None
    paypal_order_id: str | None = None
    paypal_capture_id: str | None = None
    paypal_refund_id: str | None = None
    refund_reason: str | None = None

    model_config = {"from_attributes": True}
