"""
Pruebas para las funcionalidades de la Especificación StyleStore v6:
- Dashboard de estadísticas con filtro por sucursal
- Chatbot local inteligente con chips de navegación
- Sistema de notificaciones y suscripciones (in-app y email)
- Reserva básica de prendas para todo cliente y agenda máxima de 3 días
- Cambios y Devoluciones con validación de 7 días
- Venta presencial con Efectivo y QR
- Los 8 nuevos reportes dinámicos
"""
import pytest
from datetime import date, timedelta
from decimal import Decimal
from fastapi.testclient import TestClient

from app.models.user import User
from app.models.cliente import Cliente
from app.models.sucursal import Sucursal
from app.models.categoria import Categoria
from app.models.color import Color
from app.models.talla import Talla
from app.models.producto import Producto
from app.models.producto_color import ProductoColor
from app.models.stock_inventario import StockInventario
from app.models.orden_venta import OrdenVenta, DetalleVenta
from app.models.pago import Pago
from app.models.empresa import Empresa
from app.models.cambio_devolucion import SaldoFavorCliente
from app.models.envio import Envio
from app.models.notificacion import Notificacion
from app.core.security import create_access_token
from app.services.plan_service import PlanService


@pytest.fixture
def auth_tokens(db_session):
    empresa = db_session.query(Empresa).filter(Empresa.codigo == "stylestore").one()
    """Crea un admin y un cliente con tokens de autenticación para pruebas v6."""
    # Admin
    admin = db_session.query(User).filter(User.email == "admin_v6@stylestore.com").first()
    if not admin:
        admin = User(
            email="admin_v6@stylestore.com",
            name="Admin V6",
            hashed_password="hash",
            role="administrador",
            is_active=True,
        )
        db_session.add(admin)
        db_session.commit()
        db_session.refresh(admin)

    admin.empresa_id = empresa.id
    admin.is_platform_admin = False
    admin_token = create_access_token(data={"sub": str(admin.id), "email": admin.email, "role": admin.role})

    # Cliente
    cliente_user = db_session.query(User).filter(User.email == "cliente_v6@test.com").first()
    if not cliente_user:
        cliente_user = User(
            email="cliente_v6@test.com",
            name="Cliente V6",
            hashed_password="hash",
            role="cliente",
            is_active=True,
        )
        db_session.add(cliente_user)
        db_session.commit()
        db_session.refresh(cliente_user)

    cliente_user.empresa_id = empresa.id
    cliente_user.is_platform_admin = False
    db_session.commit()
    PlanService(db_session).contract(admin, "basico")

    cliente_profile = db_session.query(Cliente).filter(Cliente.user_id == cliente_user.id).first()
    if not cliente_profile:
        cliente_profile = Cliente(
            codigo="CLI-V6-001",
            user_id=cliente_user.id,
            telefono="77712345",
            direccion="Av. Central 123",
        )
        db_session.add(cliente_profile)
        db_session.commit()
        db_session.refresh(cliente_profile)

    client_token = create_access_token(data={"sub": str(cliente_user.id), "email": cliente_user.email, "role": cliente_user.role})

    return {
        "admin": admin,
        "admin_token": admin_token,
        "client_user": cliente_user,
        "client_profile": cliente_profile,
        "client_token": client_token,
    }


@pytest.fixture
def sample_inventory(db_session):
    """Crea una sucursal, producto y stock para probar ventas y reservas."""
    empresa = db_session.query(Empresa).filter(Empresa.codigo == "stylestore").one()
    suc = db_session.query(Sucursal).filter(Sucursal.name == "Sucursal V6 Test").first()
    if not suc:
        suc = Sucursal(
            name="Sucursal V6 Test",
            city="Santa Cruz",
            address="Calle 7 Oeste #45",
            phone="33445566",
            active=True,
        )
        db_session.add(suc)
        db_session.commit()
        db_session.refresh(suc)
    suc.empresa_id = empresa.id

    cat = db_session.query(Categoria).first()
    if not cat:
        cat = Categoria(nombre="Vestidos")
        db_session.add(cat)
        db_session.commit()
        db_session.refresh(cat)
    cat.empresa_id = empresa.id

    col = db_session.query(Color).first()
    if not col:
        col = Color(nombre="Azul Marino")
        db_session.add(col)
        db_session.commit()
        db_session.refresh(col)
    col.empresa_id = empresa.id

    tal = db_session.query(Talla).first()
    if not tal:
        tal = Talla(nombre="M")
        db_session.add(tal)
        db_session.commit()
        db_session.refresh(tal)
    tal.empresa_id = empresa.id

    from app.models.temporada import Temporada
    temp = db_session.query(Temporada).first()
    if not temp:
        temp = Temporada(nombre="Verano 2026")
        db_session.add(temp)
        db_session.commit()
        db_session.refresh(temp)
    temp.empresa_id = empresa.id

    prod = db_session.query(Producto).filter(Producto.codigo == "V6-PROD-01").first()
    if not prod:
        prod = Producto(
            codigo="V6-PROD-01",
            nombre="Vestido Gala Elegance",
            descripcion="Vestido elegante",
            precio=Decimal("250.00"),
            categoria_id=cat.id,
            temporada_id=temp.id,
            active=True,
        )
        db_session.add(prod)
        db_session.commit()
        db_session.refresh(prod)
    prod.empresa_id = empresa.id
    db_session.commit()

    pcol = db_session.query(ProductoColor).filter(ProductoColor.producto_codigo == prod.codigo).first()
    if not pcol:
        pcol = ProductoColor(producto_codigo=prod.codigo, color_id=col.id)
        db_session.add(pcol)
        db_session.commit()
        db_session.refresh(pcol)

    stock = db_session.query(StockInventario).filter(
        StockInventario.sucursal_id == suc.id, StockInventario.producto_color_id == pcol.id
    ).first()
    if not stock:
        stock = StockInventario(
            sucursal_id=suc.id,
            producto_color_id=pcol.id,
            talla_id=tal.id,
            cantidad=15,
        )
        db_session.add(stock)
        db_session.commit()
        db_session.refresh(stock)

    return {"sucursal": suc, "producto": prod, "stock": stock, "talla": tal, "color": col}


def test_dashboard_stats_endpoint(client: TestClient, auth_tokens, sample_inventory):
    headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    res = client.get("/api/v1/reportes/dashboard-stats", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "ventas_hoy" in data
    assert "ventas_mes" in data
    assert "total_ingresos" in data
    assert "inventario" in data
    assert "sucursales" in data


def test_variant_price_and_measurements_flow(client: TestClient, auth_tokens, sample_inventory):
    admin_headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    client_headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    stock = sample_inventory["stock"]

    adjusted = client.post(
        "/api/v1/inventario/ajustar",
        headers=admin_headers,
        json={
            "stock_inventario_id": stock.id,
            "cantidad": 15,
            "precio_variante": 279.90,
            "medidas": {"pecho": 92, "cintura": 74, "largo": 108},
        },
    )
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["precio_variante"] == 279.9
    assert adjusted.json()["medidas"]["pecho"] == 92

    detail = client.get(
        f"/api/v1/catalogo/{sample_inventory['producto'].codigo}/detalle",
        headers=client_headers,
    )
    assert detail.status_code == 200, detail.text
    variant = detail.json()["variantes"][0]["existencias"][0]
    assert variant["precio"] == 279.9
    assert variant["medidas"]["largo"] == 108

    cart = client.post(
        "/api/v1/carrito/items",
        headers=client_headers,
        json={"stock_inventario_id": stock.id, "cantidad": 1},
    )
    assert cart.status_code == 200, cart.text
    cart_item = cart.json()["items"][0]
    assert float(cart_item["precio_unitario"]) == 279.9
    assert cart_item["producto_color_id"] == stock.producto_color_id
    assert cart_item["talla_id"] == stock.talla_id
    assert cart_item["sucursal_id"] == sample_inventory["sucursal"].id
    assert cart_item["sucursal_nombre"] == sample_inventory["sucursal"].nombre


def test_checkout_recojo_ocupa_agenda_compartida(client: TestClient, auth_tokens, sample_inventory, db_session):
    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    stock = sample_inventory["stock"]
    pickup_date = date.today() + timedelta(days=1)
    added = client.post(
        "/api/v1/carrito/items",
        headers=headers,
        json={"stock_inventario_id": stock.id, "cantidad": 1},
    )
    assert added.status_code == 200, added.text

    confirmed = client.post(
        "/api/v1/carrito/confirmar",
        headers=headers,
        json={
            "sucursal_id": sample_inventory["sucursal"].id,
            "metodo_pago": "paypal",
            "tipo_entrega": "recojo",
            "fecha_recojo": pickup_date.isoformat(),
            "hora_recojo": "10:00:00",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    orden = db_session.query(OrdenVenta).filter(OrdenVenta.id == confirmed.json()["orden_venta_id"]).one()
    assert orden.tipo_entrega == "recojo"
    assert orden.fecha_recojo == pickup_date
    assert orden.hora_recojo.strftime("%H:%M") == "10:00"

    agenda = client.get(
        "/api/v1/reservas/disponibilidad",
        headers=headers,
        params={"sucursal_id": sample_inventory["sucursal"].id, "fecha": pickup_date.isoformat()},
    )
    assert agenda.status_code == 200, agenda.text
    blocks = {block["inicio"]: block["disponible"] for block in agenda.json()["bloques"]}
    assert blocks["10:00"] is False


def test_checkout_integral_movil_programa_recojo(client: TestClient, auth_tokens, sample_inventory, db_session):
    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    pickup_date = date.today() + timedelta(days=2)
    added = client.post(
        "/api/v1/carrito/items",
        headers=headers,
        json={"stock_inventario_id": sample_inventory["stock"].id, "cantidad": 1},
    )
    assert added.status_code == 200, added.text
    result = client.post(
        "/api/v1/carrito/checkout",
        headers=headers,
        json={
            "metodo_pago": "paypal",
            "sucursal_id": sample_inventory["sucursal"].id,
            "tipo_entrega": "recojo",
            "fecha_recojo": pickup_date.isoformat(),
            "hora_recojo": "12:00:00",
        },
    )
    assert result.status_code == 200, result.text
    order = db_session.query(OrdenVenta).filter_by(id=result.json()["orden_venta_id"]).one()
    assert order.tipo_entrega == "recojo"
    assert order.hora_recojo.strftime("%H:%M") == "12:00"


def test_chatbot_local_inteligente(client: TestClient, db_session, auth_tokens):
    PlanService(db_session).contract(auth_tokens["admin"], "intermedio")
    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    
    # 1. Saludo
    res = client.post("/api/v1/chatbot/mensaje", json={"mensaje": "Hola buenas tardes"}, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "Bienvenido a StyleStore" in data["respuesta"]
    assert len(data["chips"]) > 0

    # 2. Consulta de sucursales
    res2 = client.post("/api/v1/chatbot/mensaje", json={"mensaje": "¿Dónde están sus sucursales físicas?"}, headers=headers)
    assert res2.status_code == 200
    assert "sucursales" in res2.json()["respuesta"].lower()


def test_notificaciones_usuario(client: TestClient, auth_tokens):
    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    
    # Obtener contador no leídas
    res = client.get("/api/v1/notificaciones/no-leidas-count", headers=headers)
    assert res.status_code == 200
    assert "count" in res.json()

    # Listar notificaciones
    res_list = client.get("/api/v1/notificaciones/mis-notificaciones", headers=headers)
    assert res_list.status_code == 200
    assert "items" in res_list.json()


def test_cliente_nuevo_puede_reservar_sin_compra_previa(client: TestClient, auth_tokens, sample_inventory, db_session):
    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    
    # El examen exige reservas como función básica: una cuenta nueva no puede quedar bloqueada.
    res_eleg = client.get("/api/v1/reservas/elegibilidad", headers=headers)
    assert res_eleg.status_code == 200
    eleg = res_eleg.json()
    assert eleg["puede_reservar"] is True
    assert eleg["compras_previas"] == 0

    # La reserva debe crearse sin exigir una venta anterior.
    res_reserva = client.post(
        "/api/v1/reservas",
        json={
            "sucursal_id": sample_inventory["sucursal"].id,
            "items": [{"stock_inventario_id": sample_inventory["stock"].id, "cantidad": 1}],
        },
        headers=headers,
    )
    assert res_reserva.status_code == 201, res_reserva.text
    reserva_id = res_reserva.json()["id"]
    assert res_reserva.json()["estado"] == "pendiente"

    # Verificar que el cliente ve sus reservas en /reservas/mias
    res_mias = client.get("/api/v1/reservas/mias", headers=headers)
    assert res_mias.status_code == 200
    assert any(r["id"] == reserva_id for r in res_mias.json())

    # Verificar que el staff puede listar con filtro por sucursal
    admin_headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    res_admin = client.get(f"/api/v1/reservas?sucursal_id={sample_inventory['sucursal'].id}", headers=admin_headers)
    assert res_admin.status_code == 200
    assert any(r["id"] == reserva_id for r in res_admin.json())

    # Cancelar la reserva como cliente y verificar restitución de stock
    res_cancelar = client.post(f"/api/v1/reservas/{reserva_id}/cancelar", headers=headers)
    assert res_cancelar.status_code == 200
    assert res_cancelar.json()["estado"] == "cancelada"

    # Intentar cancelar nuevamente debe fallar
    res_re_cancelar = client.post(f"/api/v1/reservas/{reserva_id}/cancelar", headers=headers)
    assert res_re_cancelar.status_code == 400


def test_reserva_debe_pagarse_en_caja_antes_de_entrega(client: TestClient, auth_tokens, sample_inventory, db_session):
    client_headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    admin_headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    stock = sample_inventory["stock"]
    stock.cantidad = 15
    db_session.commit()

    created = client.post(
        "/api/v1/reservas",
        headers=client_headers,
        json={"sucursal_id": sample_inventory["sucursal"].id,
              "items": [{"stock_inventario_id": stock.id, "cantidad": 1}]},
    )
    assert created.status_code == 201, created.text
    reserva_id = created.json()["id"]
    db_session.refresh(stock)
    assert stock.cantidad == 14

    sent = client.post(f"/api/v1/reservas/{reserva_id}/enviar-a-caja", headers=admin_headers)
    assert sent.status_code == 200, sent.text
    order_id = sent.json()["orden_venta_id"]
    db_session.refresh(stock)
    assert stock.cantidad == 14, "Enviar a caja no debe descontar dos veces el stock ya reservado"

    premature = client.patch(
        f"/api/v1/reservas/{reserva_id}/estado",
        headers=admin_headers,
        json={"estado": "completada"},
    )
    assert premature.status_code == 400

    paid = client.post(
        "/api/v1/pagos/caja",
        headers=admin_headers,
        json={"orden_venta_id": order_id, "efectivo_recibido": 250, "metodo_pago": "efectivo"},
    )
    assert paid.status_code == 200, paid.text
    listed = client.get("/api/v1/reservas", headers=admin_headers).json()
    assert next(item for item in listed if item["id"] == reserva_id)["estado"] == "pagada_pendiente_entrega"

    delivered = client.patch(
        f"/api/v1/reservas/{reserva_id}/estado",
        headers=admin_headers,
        json={"estado": "completada"},
    )
    assert delivered.status_code == 200, delivered.text


def test_reserva_respeta_horario_y_maximo_tres_dias(client: TestClient, auth_tokens, sample_inventory, db_session):
    client_headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    admin_headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    sucursal_id = sample_inventory["sucursal"].id
    stock_id = sample_inventory["stock"].id

    update_branch = client.put(
        f"/api/v1/sucursales/{sucursal_id}",
        headers=admin_headers,
        json={"hora_apertura": "10:00:00", "hora_cierre": "16:00:00"},
    )
    assert update_branch.status_code == 200, update_branch.text

    db_session.add(
        OrdenVenta(
            fecha=date.today() - timedelta(days=1),
            estado="pagada",
            total=Decimal("100.00"),
            tipo_venta="presencial",
            codigo_cliente=auth_tokens["client_profile"].codigo,
            sucursal_id=sucursal_id,
        )
    )
    db_session.commit()

    too_late = client.post(
        "/api/v1/reservas",
        headers=client_headers,
        json={
            "sucursal_id": sucursal_id,
            "fecha_limite": (date.today() + timedelta(days=4)).isoformat(),
            "hora_retiro": "11:00:00",
            "items": [{"stock_inventario_id": stock_id, "cantidad": 1}],
        },
    )
    assert too_late.status_code == 400
    assert "3 días" in too_late.json()["detail"]

    outside_hours = client.post(
        "/api/v1/reservas",
        headers=client_headers,
        json={
            "sucursal_id": sucursal_id,
            "fecha_limite": (date.today() + timedelta(days=1)).isoformat(),
            "hora_retiro": "09:30:00",
            "items": [{"stock_inventario_id": stock_id, "cantidad": 1}],
        },
    )
    assert outside_hours.status_code == 400
    assert "horario de atención" in outside_hours.json()["detail"]

    valid = client.post(
        "/api/v1/reservas",
        headers=client_headers,
        json={
            "sucursal_id": sucursal_id,
            "fecha_limite": (date.today() + timedelta(days=1)).isoformat(),
            "hora_retiro": "11:00:00",
            "items": [{"stock_inventario_id": stock_id, "cantidad": 1}],
        },
    )
    assert valid.status_code == 201, valid.text
    assert valid.json()["hora"] == "11:00:00"

    availability = client.get(
        "/api/v1/reservas/disponibilidad",
        headers=client_headers,
        params={
            "sucursal_id": sucursal_id,
            "fecha": (date.today() + timedelta(days=1)).isoformat(),
        },
    )
    assert availability.status_code == 200, availability.text
    blocks = {block["inicio"]: block["disponible"] for block in availability.json()["bloques"]}
    assert blocks["10:00"] is True
    assert blocks["11:00"] is False
    assert blocks["15:00"] is True

    occupied = client.post(
        "/api/v1/reservas",
        headers=client_headers,
        json={
            "sucursal_id": sucursal_id,
            "fecha_limite": (date.today() + timedelta(days=1)).isoformat(),
            "hora_retiro": "11:00:00",
            "items": [{"stock_inventario_id": stock_id, "cantidad": 1}],
        },
    )
    assert occupied.status_code == 400
    assert "ocupado" in occupied.json()["detail"]



def test_venta_presencial_efectivo_y_qr(client: TestClient, auth_tokens, sample_inventory):
    headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    
    # 1. Venta en Efectivo con vuelto
    payload_efectivo = {
        "codigo_cliente": auth_tokens["client_profile"].codigo,
        "sucursal_id": sample_inventory["sucursal"].id,
        "metodo_pago": "efectivo",
        "efectivo_recibido": 300.0,
        "items": [{"stock_inventario_id": sample_inventory["stock"].id, "cantidad": 1}],
    }
    res_ef = client.post("/api/v1/ventas/presencial", json=payload_efectivo, headers=headers)
    assert res_ef.status_code == 201
    v_data = res_ef.json()
    assert v_data["metodo_pago"] == "efectivo"
    assert v_data["ticket_numero"].startswith("TCK-")
    assert v_data["comprador_nombre"] == "Cliente V6"

    # 2. Venta con QR
    payload_qr = {
        "codigo_cliente": auth_tokens["client_profile"].codigo,
        "sucursal_id": sample_inventory["sucursal"].id,
        "metodo_pago": "qr",
        "items": [{"stock_inventario_id": sample_inventory["stock"].id, "cantidad": 1}],
    }
    res_qr = client.post("/api/v1/ventas/presencial", json=payload_qr, headers=headers)
    assert res_qr.status_code == 201
    assert res_qr.json()["metodo_pago"] == "qr"


def test_venta_mostrador_guarda_nombre_y_ci_sin_crear_cliente(client: TestClient, auth_tokens, sample_inventory, db_session):
    headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    clientes_antes = db_session.query(Cliente).count()
    payload = {
        "comprador_nombre": "María Pérez",
        "comprador_ci": "7845123",
        "sucursal_id": sample_inventory["sucursal"].id,
        "metodo_pago": "efectivo",
        "efectivo_recibido": 300,
        "items": [{"stock_inventario_id": sample_inventory["stock"].id, "cantidad": 1}],
    }

    response = client.post("/api/v1/ventas/presencial", json=payload, headers=headers)

    assert response.status_code == 201, response.text
    venta = response.json()
    assert venta["codigo_cliente"] is None
    assert venta["cliente_nombre"] == "María Pérez"
    assert venta["comprador_nombre"] == "María Pérez"
    assert venta["comprador_ci"] == "7845123"
    assert db_session.query(Cliente).count() == clientes_antes

    recibo = client.get(f"/api/v1/pagos/{venta['pago_id']}", headers=headers)
    assert recibo.status_code == 200
    assert recibo.json()["comprador_nombre"] == "María Pérez"
    assert recibo.json()["comprador_ci"] == "7845123"


def test_venta_mostrador_exige_identificacion_si_no_hay_cliente(client: TestClient, auth_tokens, sample_inventory):
    response = client.post(
        "/api/v1/ventas/presencial",
        headers={"Authorization": f"Bearer {auth_tokens['admin_token']}"},
        json={
            "sucursal_id": sample_inventory["sucursal"].id,
            "metodo_pago": "qr",
            "items": [{"stock_inventario_id": sample_inventory["stock"].id, "cantidad": 1}],
        },
    )
    assert response.status_code == 422


def test_cambios_devoluciones_workflow(client: TestClient, auth_tokens, sample_inventory, db_session):
    headers_cli = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    headers_adm = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}

    # Crear una orden de compra reciente (hace 2 días)
    orden_reciente = OrdenVenta(
        fecha=date.today() - timedelta(days=2),
        estado="pagada",
        total=Decimal("250.00"),
        tipo_venta="presencial",
        codigo_cliente=auth_tokens["client_profile"].codigo,
        sucursal_id=sample_inventory["sucursal"].id,
    )
    db_session.add(orden_reciente)
    db_session.flush()

    dv = DetalleVenta(
        orden_venta_id=orden_reciente.id,
        stock_inventario_id=sample_inventory["stock"].id,
        producto_nombre=sample_inventory["producto"].nombre,
        color_nombre="Azul",
        talla_nombre="M",
        cantidad=1,
        precio_unitario=Decimal("250.00"),
        subtotal=Decimal("250.00"),
    )
    db_session.add(dv)
    db_session.commit()

    # 1. Solicitar cambio (dentro del límite de 7 días)
    sol_payload = {
        "orden_venta_id": orden_reciente.id,
        "detalle_venta_id": dv.id,
        "tipo": "cambio",
        "motivo": "talla_incorrecta",
        "sucursal_id": sample_inventory["sucursal"].id,
        "fecha_programada": str(date.today() + timedelta(days=1)),
        "descripcion_problema": "La talla M me queda un poco grande, deseo cambiar por S.",
    }
    res_sol = client.post("/api/v1/cambios", json=sol_payload, headers=headers_cli)
    assert res_sol.status_code == 200
    sol_id = res_sol.json()["solicitud_id"]

    # 2. Encargado/Admin responde aprobando
    res_resp = client.put(
        f"/api/v1/cambios/{sol_id}/responder",
        json={"nuevo_estado": "aceptada", "respuesta_encargado": "Pasa por sucursal con la prenda y etiqueta."},
        headers=headers_adm,
    )
    assert res_resp.status_code == 200
    assert res_resp.json()["estado"] == "aceptada"

    # 3. El ajuste se prepara y luego se liquida en caja
    preparado = client.post(
        f"/api/v1/cambios/{sol_id}/preparar-liquidacion",
        json={"reponer_prenda_original": True},
        headers=headers_adm,
    )
    assert preparado.status_code == 200
    res_comp = client.post(f"/api/v1/cambios/{sol_id}/completar", json={}, headers=headers_adm)
    assert res_comp.status_code == 200
    assert res_comp.json()["estado"] == "completada"

    # 4. Probar que una compra de hace 15 días rechaza la solicitud (> 7 días)
    orden_antigua = OrdenVenta(
        fecha=date.today() - timedelta(days=15),
        estado="pagada",
        total=Decimal("250.00"),
        tipo_venta="presencial",
        codigo_cliente=auth_tokens["client_profile"].codigo,
        sucursal_id=sample_inventory["sucursal"].id,
    )
    db_session.add(orden_antigua)
    db_session.flush()

    dv_old = DetalleVenta(
        orden_venta_id=orden_antigua.id,
        stock_inventario_id=sample_inventory["stock"].id,
        producto_nombre=sample_inventory["producto"].nombre,
        cantidad=1,
        precio_unitario=Decimal("250.00"),
        subtotal=Decimal("250.00"),
    )
    db_session.add(dv_old)
    db_session.commit()

    sol_old = {
        "orden_venta_id": orden_antigua.id,
        "detalle_venta_id": dv_old.id,
        "tipo": "cambio",
        "motivo": "disconformidad",
        "sucursal_id": sample_inventory["sucursal"].id,
        "fecha_programada": str(date.today() + timedelta(days=1)),
        "descripcion_problema": "Deseo cambiarlo.",
    }
    res_old = client.post("/api/v1/cambios", json=sol_old, headers=headers_cli)
    assert res_old.status_code == 400
    assert "7 días" in res_old.json()["detail"]


def test_cambio_menor_genera_reembolso_pendiente_y_nota_venta(client: TestClient, auth_tokens, sample_inventory, db_session):
    headers_cli = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    headers_adm = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    stock = sample_inventory["stock"]
    stock.precio_variante = Decimal("150.00")
    stock.cantidad = max(stock.cantidad, 2)
    orden = OrdenVenta(
        fecha=date.today(), estado="pagada", total=Decimal("250.00"), tipo_venta="presencial",
        codigo_cliente=auth_tokens["client_profile"].codigo, sucursal_id=sample_inventory["sucursal"].id,
    )
    db_session.add(orden); db_session.flush()
    detalle = DetalleVenta(
        orden_venta_id=orden.id, stock_inventario_id=stock.id,
        producto_nombre=sample_inventory["producto"].nombre, cantidad=1,
        precio_unitario=Decimal("250.00"), subtotal=Decimal("250.00"),
    )
    db_session.add(detalle); db_session.commit()
    solicitud = client.post("/api/v1/cambios", json={
        "orden_venta_id": orden.id, "detalle_venta_id": detalle.id, "tipo": "cambio",
        "motivo": "talla_incorrecta", "sucursal_id": sample_inventory["sucursal"].id,
        "fecha_programada": str(date.today()), "descripcion_problema": "Cambio por otra prenda",
    }, headers=headers_cli)
    assert solicitud.status_code == 200
    solicitud_id = solicitud.json()["solicitud_id"]
    assert client.put(f"/api/v1/cambios/{solicitud_id}/responder", json={
        "nuevo_estado": "aceptada", "respuesta_encargado": "Aprobado",
    }, headers=headers_adm).status_code == 200

    preparado = client.post(f"/api/v1/cambios/{solicitud_id}/preparar-liquidacion", json={
        "reponer_prenda_original": True, "nuevo_stock_inventario_id": stock.id,
    }, headers=headers_adm)
    assert preparado.status_code == 200, preparado.text
    assert Decimal(str(preparado.json()["monto_reembolsado"])) == Decimal("100.00")
    completado = client.post(f"/api/v1/cambios/{solicitud_id}/completar", json={
        "metodo_reembolso": "efectivo",
    }, headers=headers_adm)
    assert completado.status_code == 200, completado.text
    data = completado.json()
    assert Decimal(str(data["precio_original"])) == Decimal("250.00")
    assert Decimal(str(data["precio_nuevo"])) == Decimal("150.00")
    assert Decimal(str(data["saldo_favor_generado"])) == Decimal("0.00")
    assert Decimal(str(data["monto_reembolsado"])) == Decimal("100.00")
    assert data["nota_venta_numero"].startswith("NCJ-")
    comprobante = data["comprobante"]
    assert comprobante["tipo_documento"] == "COMPROBANTE DE CAMBIO"
    assert Decimal(str(comprobante["original"]["precio"])) == Decimal("250.00")
    assert comprobante["reemplazo"]["producto"] == sample_inventory["producto"].nombre
    assert Decimal(str(comprobante["reemplazo"]["precio"])) == Decimal("150.00")
    assert Decimal(str(comprobante["diferencia_devuelta"])) == Decimal("100.00")

    pago_salida = db_session.query(Pago).filter(Pago.orden_venta_id == data["orden_ajuste_id"]).one()
    assert pago_salida.estado == "reembolsado"
    assert pago_salida.tipo_pago == "devolucion en caja"


def test_tenant_configura_plazo_de_cambios(client: TestClient, auth_tokens):
    headers_adm = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    response = client.put("/api/v1/cambios/politica", json={"plazo_cambios_dias": 15}, headers=headers_adm)
    assert response.status_code == 200
    assert response.json()["plazo_cambios_dias"] == 15


def test_cambio_mayor_exige_y_registra_cobro(client: TestClient, auth_tokens, sample_inventory, db_session):
    headers_cli = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    headers_adm = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    stock = sample_inventory["stock"]
    stock.precio_variante = Decimal("250.00")
    stock.cantidad = max(stock.cantidad, 2)
    orden = OrdenVenta(fecha=date.today(), estado="pagada", total=Decimal("100.00"), tipo_venta="presencial", codigo_cliente=auth_tokens["client_profile"].codigo, sucursal_id=sample_inventory["sucursal"].id)
    db_session.add(orden); db_session.flush()
    detalle = DetalleVenta(orden_venta_id=orden.id, stock_inventario_id=stock.id, producto_nombre="Prenda original", cantidad=1, precio_unitario=Decimal("100.00"), subtotal=Decimal("100.00"))
    db_session.add(detalle); db_session.commit()
    res = client.post("/api/v1/cambios", json={"orden_venta_id": orden.id, "detalle_venta_id": detalle.id, "tipo": "cambio", "motivo": "otro", "sucursal_id": sample_inventory["sucursal"].id, "fecha_programada": str(date.today()), "descripcion_problema": "Desea prenda superior"}, headers=headers_cli)
    solicitud_id = res.json()["solicitud_id"]
    client.put(f"/api/v1/cambios/{solicitud_id}/responder", json={"nuevo_estado": "aceptada", "respuesta_encargado": "Aprobado"}, headers=headers_adm)

    preparado = client.post(f"/api/v1/cambios/{solicitud_id}/preparar-liquidacion", json={"nuevo_stock_inventario_id": stock.id}, headers=headers_adm)
    assert preparado.status_code == 200, preparado.text
    sin_pago = client.post(f"/api/v1/cambios/{solicitud_id}/completar", json={}, headers=headers_adm)
    assert sin_pago.status_code == 400
    completado = client.post(f"/api/v1/cambios/{solicitud_id}/completar", json={"metodo_pago_diferencia": "efectivo", "efectivo_recibido": 200}, headers=headers_adm)
    assert completado.status_code == 200, completado.text
    assert Decimal(str(completado.json()["monto_adicional"])) == Decimal("150.00")
    orden_ajuste_id = completado.json()["orden_ajuste_id"]
    assert db_session.query(Pago).filter(Pago.orden_venta_id == orden_ajuste_id, Pago.monto == Decimal("150.00")).one()


def test_cambio_entre_variantes_repone_original_y_descuenta_reemplazo(
    client: TestClient, auth_tokens, sample_inventory, db_session
):
    """Regresión: un cambio debe mover las dos existencias, no solo la devuelta."""
    headers_cli = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    headers_adm = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    original = sample_inventory["stock"]
    original.cantidad = 4
    replacement_size = Talla(
        nombre=f"Cambio-{original.id}",
        empresa_id=sample_inventory["producto"].empresa_id,
    )
    db_session.add(replacement_size)
    db_session.flush()
    replacement = StockInventario(
        sucursal_id=original.sucursal_id,
        producto_color_id=original.producto_color_id,
        talla_id=replacement_size.id,
        cantidad=7,
        precio_variante=Decimal("275.00"),
    )
    db_session.add(replacement)
    order = OrdenVenta(
        fecha=date.today(), estado="pagada", total=Decimal("250.00"), tipo_venta="presencial",
        codigo_cliente=auth_tokens["client_profile"].codigo, sucursal_id=original.sucursal_id,
    )
    db_session.add(order)
    db_session.flush()
    detail = DetalleVenta(
        orden_venta_id=order.id, stock_inventario_id=original.id,
        producto_nombre=sample_inventory["producto"].nombre, cantidad=1,
        precio_unitario=Decimal("250.00"), subtotal=Decimal("250.00"),
    )
    db_session.add(detail)
    db_session.commit()

    requested = client.post("/api/v1/cambios", json={
        "orden_venta_id": order.id, "detalle_venta_id": detail.id, "tipo": "cambio",
        "motivo": "talla_incorrecta", "sucursal_id": original.sucursal_id,
        "fecha_programada": str(date.today()), "descripcion_problema": "Cambio por otra talla",
    }, headers=headers_cli)
    assert requested.status_code == 200, requested.text
    request_id = requested.json()["solicitud_id"]
    assert client.put(f"/api/v1/cambios/{request_id}/responder", json={
        "nuevo_estado": "aceptada", "respuesta_encargado": "Aprobado",
    }, headers=headers_adm).status_code == 200
    prepared = client.post(f"/api/v1/cambios/{request_id}/preparar-liquidacion", json={
        "reponer_prenda_original": True, "nuevo_stock_inventario_id": replacement.id,
    }, headers=headers_adm)
    assert prepared.status_code == 200, prepared.text
    completed = client.post(f"/api/v1/cambios/{request_id}/completar", json={
        "metodo_pago_diferencia": "efectivo", "efectivo_recibido": 25,
    }, headers=headers_adm)
    assert completed.status_code == 200, completed.text

    db_session.expire_all()
    assert db_session.get(StockInventario, original.id).cantidad == 5
    assert db_session.get(StockInventario, replacement.id).cantidad == 6

    history = client.get("/api/v1/ventas/mis-compras", headers=headers_cli)
    assert history.status_code == 200, history.text
    original_order = next(item for item in history.json() if item["id"] == order.id)
    assert original_order["cambios_devoluciones"][0]["estado"] == "completada"
    assert original_order["cambios_devoluciones"][0]["talla_reemplazo"] == replacement_size.nombre

    staff_history = client.get("/api/v1/cambios", headers=headers_adm)
    assert staff_history.status_code == 200, staff_history.text
    completed_request = next(item for item in staff_history.json() if item["id"] == request_id)
    assert completed_request["estado"] == "completada"
    assert completed_request["nota_venta_numero"]


def test_dashboard_entrega_detalle_especifico_por_indicador(client: TestClient, auth_tokens, sample_inventory):
    headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    expected_titles = {
        "reservas_vigentes": "Reservas y disponibilidad comprometida",
        "cambios_devoluciones": "Cambios y devoluciones",
        "pagos_caja": "Cobros registrados en caja",
        "pagos_paypal": "Pagos digitales PayPal",
    }
    for indicator, title in expected_titles.items():
        response = client.get(
            f"/api/v1/reportes/dashboard-indicadores/{indicator}", headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["indicador"] == indicator
        assert body["titulo"] == title
        assert body["titulo"] != "Historial de ventas"


def test_nuevos_reportes_previews(client: TestClient, auth_tokens):
    headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    
    endpoints = [
        "/api/v1/reportes/mas-vendidos/preview",
        "/api/v1/reportes/ventas-producto/preview",
        "/api/v1/reportes/ventas-tipo/preview",
        "/api/v1/reportes/clientes/preview",
        "/api/v1/reportes/empleados/preview",
        "/api/v1/reportes/rotacion/preview",
        "/api/v1/reportes/envios/preview",
        "/api/v1/reportes/devoluciones/preview",
    ]
    for ep in endpoints:
        res = client.get(ep, headers=headers)
        assert res.status_code == 200, f"Error en endpoint {ep}: {res.text}"


def test_catalogo_para_ti_y_sucursales_publicas(client: TestClient, auth_tokens):
    """Verifica que el feed 'Para Ti' con IA y el listado de sucursales funcionen para clientes."""
    headers_cli = {"Authorization": f"Bearer {auth_tokens['client_token']}"}

    # 1. Endpoint /api/v1/catalogo/para-ti
    res_para_ti = client.get("/api/v1/catalogo/para-ti?limit=4", headers=headers_cli)
    assert res_para_ti.status_code == 200
    data_para_ti = res_para_ti.json()
    assert isinstance(data_para_ti, list)

    # 2. Endpoint /api/v1/sucursales accesible para cliente
    res_suc = client.get("/api/v1/sucursales", headers=headers_cli)
    assert res_suc.status_code == 200
    assert isinstance(res_suc.json(), list)


def test_asistente_ia_reportes(client: TestClient, db_session, auth_tokens):
    """Verifica rol y plan antes de responder reportes con IA."""
    client_headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    forbidden_role = client.post(
        "/api/v1/reportes/asistente-ia",
        json={"pregunta": "¿Cuánto se vendió hoy?"},
        headers=client_headers,
    )
    assert forbidden_role.status_code == 403

    empresa = db_session.query(Empresa).filter(Empresa.codigo == "stylestore").one()
    admin = auth_tokens["admin"]
    admin.empresa_id = empresa.id
    db_session.commit()
    plans = PlanService(db_session)
    plans.ensure_tenant_plans(empresa.id)
    plans.contract(admin, "basico")

    headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    forbidden_plan = client.post(
        "/api/v1/reportes/asistente-ia",
        json={"pregunta": "¿Cuánto se vendió hoy?"},
        headers=headers,
    )
    assert forbidden_plan.status_code == 403

    plans.contract(admin, "profesional")
    res = client.post(
        "/api/v1/reportes/asistente-ia",
        json={"pregunta": "¿Cuántas ropas se vendieron hoy y cuántos pedidos por delivery hubo?"},
        headers=headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert "respuesta" in data
    assert "kpis" in data
    assert isinstance(data["kpis"], list)
def test_favorites_are_idempotent_and_exposed_in_catalog(
    client: TestClient, auth_tokens, sample_inventory, db_session
):
    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    code = sample_inventory["producto"].codigo

    first = client.post(f"/api/v1/favoritos/{code}", headers=headers)
    second = client.post(f"/api/v1/favoritos/{code}", headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201

    favorites = client.get("/api/v1/favoritos", headers=headers)
    assert favorites.status_code == 200
    assert [item["producto_codigo"] for item in favorites.json()] == [code]

    catalog = client.get("/api/v1/catalogo", headers=headers)
    assert catalog.status_code == 200
    product = next(item for item in catalog.json() if item["codigo"] == code)
    assert product["es_favorito"] is True

    removed = client.delete(f"/api/v1/favoritos/{code}", headers=headers)
    assert removed.status_code == 200
    assert removed.json()["es_favorito"] is False
    assert client.get("/api/v1/favoritos", headers=headers).json() == []


def test_favorites_reject_products_from_another_tenant(
    client: TestClient, auth_tokens, sample_inventory, db_session
):
    other = Empresa(codigo="favoritos-otro", nombre="Favoritos Otro Tenant", active=True)
    db_session.add(other)
    db_session.flush()
    sample_inventory["producto"].empresa_id = other.id
    db_session.commit()

    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}
    response = client.post(
        f"/api/v1/favoritos/{sample_inventory['producto'].codigo}", headers=headers
    )
    assert response.status_code == 404


def _completed_order_for_experience(db_session, auth_tokens, sample_inventory):
    order = OrdenVenta(
        fecha=date.today(), estado="entregada", total=Decimal("250.00"),
        tipo_venta="en linea", codigo_cliente=auth_tokens["client_profile"].codigo,
        sucursal_id=sample_inventory["sucursal"].id, metodo_pago="paypal",
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def test_low_rating_persists_without_claim_and_can_create_one_later(
    client: TestClient, auth_tokens, sample_inventory, db_session
):
    order = _completed_order_for_experience(db_session, auth_tokens, sample_inventory)
    client_headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}

    rating = client.post("/api/v1/experiencia/calificaciones", headers=client_headers, json={
        "orden_venta_id": order.id, "puntuacion": 2,
        "comentario": "La atención tardó demasiado.",
    })
    assert rating.status_code == 201
    assert rating.json()["sugerir_reclamo"] is True
    rating_id = rating.json()["id"]

    # Cerrar/cancelar la sugerencia en UI no llama a ningún borrado: la nota persiste.
    saved = client.get("/api/v1/experiencia/calificaciones", headers=client_headers)
    assert saved.status_code == 200
    assert saved.json()[0]["id"] == rating_id

    claim = client.post("/api/v1/experiencia/reclamos", headers=client_headers, json={
        "orden_venta_id": order.id, "calificacion_id": rating_id,
        "sucursal_id": sample_inventory["sucursal"].id,
        "fecha_incidente": date.today().isoformat(), "motivo": "atencion",
        "descripcion": "Esperé más de una hora y no recibí una explicación clara.",
    })
    assert claim.status_code == 201
    assert claim.json()["estado"] == "recibido"
    assert claim.json()["calificacion_id"] == rating_id

    admin_headers = {"Authorization": f"Bearer {auth_tokens['admin_token']}"}
    updated = client.patch(
        f"/api/v1/experiencia/reclamos/{claim.json()['id']}", headers=admin_headers,
        json={"estado": "en_revision", "respuesta": "Estamos revisando lo sucedido."},
    )
    assert updated.status_code == 200
    assert updated.json()["respuesta"] == "Estamos revisando lo sucedido."


def test_delivery_completion_notifies_once_with_experience_link(
    client: TestClient, auth_tokens, sample_inventory, db_session
):
    order = _completed_order_for_experience(db_session, auth_tokens, sample_inventory)
    shipment = Envio(
        orden_venta_id=order.id, direccion="Av. Siempre Viva 123",
        ciudad="Santa Cruz", costo=Decimal("15.00"), estado="en camino",
    )
    db_session.add(shipment)
    db_session.commit()
    db_session.refresh(shipment)
    headers = {"Authorization": f"Bearer {auth_tokens['client_token']}"}

    assert client.patch(f"/api/v1/envios/{shipment.id}/completar", headers=headers).status_code == 200
    assert client.patch(f"/api/v1/envios/{shipment.id}/completar", headers=headers).status_code == 200

    notifications = db_session.query(Notificacion).filter(
        Notificacion.user_id == auth_tokens["client_user"].id,
        Notificacion.tipo == "experiencia_poscompra",
        Notificacion.url_accion == f"/cuenta/experiencia?orden_id={order.id}",
    ).all()
    assert len(notifications) == 1
