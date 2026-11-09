"""
Catálogo interactivo endpoints (v5 sección 15).
Muestra productos visibles en catálogo con colores, tallas y stock disponible en tiempo real.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.producto import Producto
from app.models.stock_inventario import StockInventario
from app.models.producto_color import ProductoColor
from app.models.color import Color
from app.models.talla import Talla
from app.models.sucursal import Sucursal
from app.models.empresa import Empresa
from app.models.user import User
from app.models.favorito import Favorito
from app.api.deps import get_current_user, get_optional_current_user
from app.core.exceptions import NotFoundException
from app.services.promocion_service import PromocionService
from app.services.catalog_pdf_service import CatalogPdfService
from app.services.offline_checkout_service import OfflineCheckoutService
from app.services.tenant_scope import effective_empresa_id

router = APIRouter(prefix="/catalogo", tags=["Catálogo"])


def _resolve_empresa_id(
    db: Session, current_user: User | None, sucursal_id: int | None = None
) -> int | None:
    if sucursal_id is not None:
        sucursal = db.query(Sucursal).filter(Sucursal.id == sucursal_id, Sucursal.active.is_(True)).first()
        if not sucursal:
            raise NotFoundException("Sucursal no encontrada.")
        scoped_empresa_id = effective_empresa_id(current_user) if current_user else None
        if scoped_empresa_id is not None and sucursal.empresa_id != scoped_empresa_id:
            raise NotFoundException("Sucursal no encontrada.")
        return sucursal.empresa_id
    if current_user:
        return effective_empresa_id(current_user)
    default_empresa = db.query(Empresa).filter(Empresa.codigo == "stylestore", Empresa.active.is_(True)).first()
    return default_empresa.id if default_empresa else None


@router.get("", summary="Listar catálogo interactivo de productos")
async def get_catalogo(
    categoria_id: int | None = Query(None),
    temporada_id: int | None = Query(None),
    coleccion_id: int | None = Query(None),
    sucursal_id: int | None = Query(None),
    search: str | None = Query(None),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Obtiene los productos activos visibles en catálogo,
    incluyendo variantes de color, tallas disponibles y stock por sucursal.
    """
    empresa_id = _resolve_empresa_id(db, current_user, sucursal_id)
    query = db.query(Producto).filter(
        Producto.active.is_(True), Producto.visible_en_catalogo.is_(True), Producto.empresa_id == empresa_id
    )

    if categoria_id:
        query = query.filter(Producto.categoria_id == categoria_id)
    if temporada_id:
        query = query.filter(Producto.temporada_id == temporada_id)
    if coleccion_id:
        query = query.filter(Producto.coleccion_id == coleccion_id)
    if search:
        query = query.filter(
            (Producto.nombre.ilike(f"%{search}%")) | (Producto.descripcion.ilike(f"%{search}%"))
        )

    prods = query.order_by(Producto.nombre.asc()).all()
    favorite_codes: set[str] = set()
    if current_user and empresa_id is not None and not current_user.is_platform_admin:
        favorite_codes = {
            code for (code,) in db.query(Favorito.producto_codigo).filter(
                Favorito.user_id == current_user.id,
                Favorito.empresa_id == empresa_id,
            ).all()
        }
    result = []

    for p in prods:
        # Extraer variantes de color y stock
        variantes = []
        stock_total = 0

        for pc in p.colores_rel:
            stock_query = db.query(StockInventario).filter(StockInventario.producto_color_id == pc.id)
            if sucursal_id:
                stock_query = stock_query.filter(StockInventario.sucursal_id == sucursal_id)

            stocks = stock_query.all()
            tallas_stock = []
            for s in stocks:
                stock_total += s.cantidad
                precio_original, precio_stock, _ = PromocionService(db).precio_stock(s)
                offline_checkout_links: dict[str, str] = {}
                if (
                    current_user
                    and current_user.role == "cliente"
                    and not current_user.is_platform_admin
                    and current_user.empresa_id == empresa_id
                    and s.cantidad > 0
                ):
                    for quantity in range(1, min(s.cantidad, 5) + 1):
                        token = OfflineCheckoutService.create_token(current_user, s.id, quantity)
                        offline_checkout_links[str(quantity)] = OfflineCheckoutService.public_url(token)
                tallas_stock.append({
                    "stock_inventario_id": s.id,
                    "talla_id": s.talla_id,
                    "talla_nombre": s.talla.nombre if s.talla else None,
                    "sucursal_id": s.sucursal_id,
                    "sucursal_nombre": s.sucursal.nombre if s.sucursal else None,
                    "sucursal_ciudad": s.sucursal.ciudad if s.sucursal else None,
                    "cantidad": s.cantidad,
                    "precio": float(precio_stock),
                    "precio_original": float(precio_original),
                    "precio_variante": float(s.precio_variante) if s.precio_variante is not None else None,
                    "medidas": s.medidas or {},
                    "offline_checkout_links": offline_checkout_links,
                })

            if pc.color:
                variantes.append({
                    "producto_color_id": pc.id,
                    "color_id": pc.color.id,
                    "color_nombre": pc.color.nombre,
                    "color_hex": getattr(pc.color, "codigo_hex", None) or getattr(pc.color, "hex", None),
                    "existencias": tallas_stock,
                })

        if sucursal_id is not None and stock_total <= 0:
            continue

        pct_desc = PromocionService(db).porcentaje_producto(p)
        en_promo = pct_desc > 0
        precio_desc = round(float(p.precio) * (100 - pct_desc) / 100, 2) if en_promo else None

        result.append({
            "codigo": p.codigo,
            "nombre": p.nombre,
            "descripcion": p.descripcion,
            "foto": p.foto,
            "foto_trasera": getattr(p, "foto_trasera", None),
            "foto_vestidor_frontal": getattr(p, "foto_vestidor_frontal", None),
            "foto_vestidor_trasera": getattr(p, "foto_vestidor_trasera", None),
            "tipo_prenda": getattr(p, "tipo_prenda", "superior") or "superior",
            "puntos_clave_ia": getattr(p, "puntos_clave_ia", None),
            "precio": float(p.precio),
            "categoria_id": p.categoria_id,
            "categoria_nombre": p.categoria.nombre if p.categoria else None,
            "temporada_id": p.temporada_id,
            "temporada_nombre": p.temporada.nombre if p.temporada else None,
            "coleccion_id": p.coleccion_id,
            "coleccion_nombre": p.coleccion.nombre if p.coleccion else None,
            "en_promocion": en_promo,
            "porcentaje_descuento": pct_desc,
            "precio_descuento": precio_desc,
            "titulo_promocion": getattr(p, "titulo_promocion", None),
            "variantes": variantes,
            "stock_total": stock_total,
            "es_favorito": p.codigo in favorite_codes,
        })

    return result


@router.get("/exportar-pdf", summary="Descargar catálogo PDF con identidad del tenant")
async def export_catalog_pdf(
    categoria_id: int | None = Query(None),
    temporada_id: int | None = Query(None),
    coleccion_id: int | None = Query(None),
    sucursal_id: int | None = Query(None),
    search: str | None = Query(None, max_length=120),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        content, filename = CatalogPdfService(db, current_user).generate(
            categoria_id=categoria_id,
            temporada_id=temporada_id,
            coleccion_id=coleccion_id,
            sucursal_id=sucursal_id,
            search=search,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


from app.services.recommendation_service import RecommendationService


@router.get("/para-ti", summary="Feed de recomendaciones inteligentes Para Ti con IA local")
async def get_catalogo_para_ti(
    limit: int = Query(6, ge=1, le=12),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Feed 'Para Ti' con IA Local: personaliza recomendaciones afines basadas
    en el perfil del cliente o sugiere prendas de alta afinidad de colección.
    """
    service = RecommendationService(db)
    user_id = current_user.id if current_user else None
    empresa_id = _resolve_empresa_id(db, current_user)
    return service.get_para_ti_recommendations(user_id=user_id, limit=limit, empresa_id=empresa_id)


@router.get("/{codigo}/detalle", summary="Obtener detalle completo de un producto para compra")
async def get_producto_detalle(
    codigo: str,
    sucursal_id: int | None = Query(None),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Obtiene la ficha técnica y comercial del producto con todas sus variantes de color,
    tallas asociadas y existencias de stock disponibles para venta directa o carrito.
    Permite filtrar existencias por sucursal específica.
    """
    # Búsqueda insensible a mayúsculas/minúsculas y espacios
    clean_cod = codigo.strip().lower()
    empresa_id = _resolve_empresa_id(db, current_user, sucursal_id)
    p = db.query(Producto).filter(
        func.lower(Producto.codigo) == clean_cod, Producto.empresa_id == empresa_id
    ).first()
    if not p:
        raise NotFoundException(f"Producto con código '{codigo}' no encontrado.")

    variantes = []
    stock_total = 0

    if p.colores_rel:
        for pc in p.colores_rel:
            stock_query = db.query(StockInventario).filter(StockInventario.producto_color_id == pc.id)
            if sucursal_id:
                stock_query = stock_query.filter(StockInventario.sucursal_id == sucursal_id)
            stocks = stock_query.all()
            tallas_stock = []
            for s in stocks:
                stock_total += s.cantidad
                precio_original, precio_stock, _ = PromocionService(db).precio_stock(s)
                offline_checkout_links: dict[str, str] = {}
                if (
                    current_user
                    and current_user.role == "cliente"
                    and not current_user.is_platform_admin
                    and current_user.empresa_id == empresa_id
                    and s.cantidad > 0
                ):
                    for quantity in range(1, min(s.cantidad, 5) + 1):
                        token = OfflineCheckoutService.create_token(current_user, s.id, quantity)
                        offline_checkout_links[str(quantity)] = OfflineCheckoutService.public_url(token)
                tallas_stock.append({
                    "stock_inventario_id": s.id,
                    "talla_id": s.talla_id,
                    "talla_nombre": s.talla.nombre if s.talla else None,
                    "sucursal_id": s.sucursal_id,
                    "sucursal_nombre": s.sucursal.nombre if s.sucursal else None,
                    "sucursal_ciudad": s.sucursal.ciudad if s.sucursal else None,
                    "cantidad": s.cantidad,
                    "precio": float(precio_stock),
                    "precio_original": float(precio_original),
                    "precio_variante": float(s.precio_variante) if s.precio_variante is not None else None,
                    "medidas": s.medidas or {},
                    "offline_checkout_links": offline_checkout_links,
                })

            if pc.color:
                variantes.append({
                    "producto_color_id": pc.id,
                    "color_id": pc.color.id,
                    "color_nombre": pc.color.nombre,
                    "color_hex": getattr(pc.color, "codigo_hex", None) or getattr(pc.color, "hex", None),
                    "existencias": tallas_stock,
                })

    # Si no tiene variantes de color asociadas, agregamos una variante estándar
    if not variantes:
        variantes.append({
            "producto_color_id": 0,
            "color_id": 0,
            "color_nombre": "Único",
            "color_hex": "#14263D",
            "existencias": [],
        })

    pct_desc_det = PromocionService(db).porcentaje_producto(p)
    en_promo_det = pct_desc_det > 0
    precio_desc_det = round(float(p.precio) * (100 - pct_desc_det) / 100, 2) if en_promo_det else None
    es_favorito = False
    if current_user and empresa_id is not None and not current_user.is_platform_admin:
        es_favorito = db.query(Favorito.id).filter(
            Favorito.user_id == current_user.id,
            Favorito.empresa_id == empresa_id,
            Favorito.producto_codigo == p.codigo,
        ).first() is not None

    return {
        "codigo": p.codigo,
        "nombre": p.nombre,
        "descripcion": p.descripcion,
        "foto": p.foto,
        "foto_trasera": getattr(p, "foto_trasera", None),
        "foto_vestidor_frontal": getattr(p, "foto_vestidor_frontal", None),
        "foto_vestidor_trasera": getattr(p, "foto_vestidor_trasera", None),
        "tipo_prenda": getattr(p, "tipo_prenda", "superior") or "superior",
        "puntos_clave_ia": getattr(p, "puntos_clave_ia", None),
        "precio": float(p.precio),
        "categoria_id": p.categoria_id,
        "categoria_nombre": p.categoria.nombre if p.categoria else None,
        "temporada_id": p.temporada_id,
        "temporada_nombre": p.temporada.nombre if p.temporada else None,
        "coleccion_id": p.coleccion_id,
        "coleccion_nombre": p.coleccion.nombre if p.coleccion else None,
        "en_promocion": en_promo_det,
        "porcentaje_descuento": pct_desc_det,
        "precio_descuento": precio_desc_det,
        "titulo_promocion": getattr(p, "titulo_promocion", None),
        "variantes": variantes,
        "stock_total": stock_total,
        "es_favorito": es_favorito,
    }


@router.get("/{codigo}/recomendados", summary="Recomendaciones de IA local para un producto")
async def get_producto_recomendados(
    codigo: str,
    limit: int = Query(4, ge=1, le=12),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Analiza las características del producto y genera sugerencias afines mediante
    el motor de IA local de StyleStore.
    """
    service = RecommendationService(db)
    empresa_id = _resolve_empresa_id(db, current_user)
    return service.get_recommendations_for_product(codigo=codigo, limit=limit, empresa_id=empresa_id)
