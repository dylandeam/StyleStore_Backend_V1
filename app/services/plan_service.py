"""Business rules for configurable subscription plans."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.models.empresa import Empresa
from app.models.plan_suscripcion import ContratacionPlan, PlanSuscripcion
from app.models.sucursal import Sucursal
from app.models.user import User
from app.schemas.plan_suscripcion import ContratacionCreate, PLAN_CODES, PlanCreate, PlanUpdate
from app.services.bitacora_service import BitacoraService


DEFAULT_PLANS = (
    {
        "codigo": "basico",
        "nombre": "Básico",
        "max_sucursales": 2,
        "ia_generativa": False,
        "capacidades": {
            "productos": True, "ventas": True, "reservas": True,
            "offline": True, "reportes": True, "chatbot": False,
            "predicciones": False, "ia_generativa": False,
        },
    },
    {
        "codigo": "intermedio",
        "nombre": "Intermedio",
        "max_sucursales": 5,
        "ia_generativa": False,
        "capacidades": {
            "productos": True, "ventas": True, "reservas": True,
            "offline": True, "reportes": True, "chatbot": True,
            "predicciones": False, "ia_generativa": False,
        },
    },
    {
        "codigo": "profesional",
        "nombre": "Profesional",
        "max_sucursales": 10,
        "ia_generativa": True,
        "capacidades": {
            "productos": True, "ventas": True, "reservas": True,
            "offline": True, "reportes": True, "chatbot": True,
            "predicciones": True, "ia_generativa": True,
        },
    },
)

MANDATORY_CAPABILITIES = {
    "productos": True,
    "ventas": True,
    "reservas": True,
    "offline": True,
    "reportes": True,
}


class PlanService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def scope_key(empresa_id: int | None) -> str:
        return "global" if empresa_id is None else f"empresa:{empresa_id}"

    def seed_global_plans(self) -> None:
        definitions = {item["codigo"]: item for item in DEFAULT_PLANS}
        for legacy in self.db.query(PlanSuscripcion).filter(
            PlanSuscripcion.codigo == "personalizado"
        ).all():
            existing = self.db.query(PlanSuscripcion).filter(
                PlanSuscripcion.scope_key == legacy.scope_key,
                PlanSuscripcion.codigo == "intermedio",
            ).first()
            if existing:
                self.db.query(ContratacionPlan).filter(
                    ContratacionPlan.plan_id == legacy.id
                ).update({ContratacionPlan.plan_id: existing.id}, synchronize_session=False)
                self.db.delete(legacy)
            else:
                legacy.codigo = "intermedio"
                legacy.nombre = "Intermedio"

        self.db.flush()

        for definition in DEFAULT_PLANS:
            exists = self.db.query(PlanSuscripcion).filter(
                PlanSuscripcion.scope_key == "global",
                PlanSuscripcion.codigo == definition["codigo"],
            ).first()
            if not exists:
                self.db.add(PlanSuscripcion(
                    empresa_id=None,
                    scope_key="global",
                    precio_mensual=0,
                    moneda="BOB",
                    beneficios=[],
                    estilo={},
                    active=True,
                    **definition,
                ))
        self.db.flush()
        for plan in self.db.query(PlanSuscripcion).filter(
            PlanSuscripcion.codigo.in_(PLAN_CODES)
        ).all():
            definition = definitions[plan.codigo]
            if plan.codigo == "basico":
                plan.max_sucursales = 2
            elif plan.codigo == "intermedio":
                plan.max_sucursales = 5
            else:
                plan.max_sucursales = max(plan.max_sucursales or 10, 10)
            plan.ia_generativa = definition["ia_generativa"]
            plan.capacidades = dict(definition["capacidades"])
            if plan.codigo == "intermedio" and plan.nombre == "Personalizado":
                plan.nombre = "Intermedio"
        self.db.commit()

    def ensure_tenant_plans(self, empresa_id: int) -> None:
        self.seed_global_plans()
        tenant_codes = {
            row[0] for row in self.db.query(PlanSuscripcion.codigo).filter(
                PlanSuscripcion.empresa_id == empresa_id
            ).all()
        }
        globals_by_code = {
            plan.codigo: plan for plan in self.db.query(PlanSuscripcion).filter(
                PlanSuscripcion.scope_key == "global"
            ).all()
        }
        for code in set(globals_by_code) - tenant_codes:
            source = globals_by_code[code]
            self.db.add(PlanSuscripcion(
                empresa_id=empresa_id,
                scope_key=self.scope_key(empresa_id),
                codigo=source.codigo,
                nombre=source.nombre,
                descripcion=source.descripcion,
                precio_mensual=source.precio_mensual,
                moneda=source.moneda,
                max_sucursales=source.max_sucursales,
                ia_generativa=source.ia_generativa,
                beneficios=list(source.beneficios or []),
                capacidades=dict(source.capacidades or {}),
                estilo=dict(source.estilo or {}),
                active=source.active,
            ))
        self.db.commit()

    def list_plans(self, empresa_id: int | None, *, active_only: bool = False):
        if empresa_id is None:
            self.seed_global_plans()
        else:
            self.ensure_tenant_plans(empresa_id)
        query = self.db.query(PlanSuscripcion).filter(
            PlanSuscripcion.scope_key == self.scope_key(empresa_id)
        )
        if active_only:
            query = query.filter(PlanSuscripcion.active.is_(True))
        order = {"basico": 0, "intermedio": 1, "profesional": 2}
        return sorted(query.all(), key=lambda item: (order.get(item.codigo, 100), item.nombre.lower()))

    def resolve_admin_scope(self, current_user: User, empresa_id: int | None) -> int | None:
        if not current_user.is_platform_admin:
            raise ForbiddenException("Solo el administrador SaaS puede definir precios y características de los planes.")
        if empresa_id is not None and not self.db.query(Empresa.id).filter(Empresa.id == empresa_id).first():
            raise NotFoundException("Empresa no encontrada.")
        return empresa_id

    def update_plan(self, codigo: str, payload: PlanUpdate, current_user: User, empresa_id: int | None = None):
        code = codigo.strip().lower()
        scope_empresa_id = self.resolve_admin_scope(current_user, empresa_id)
        plans = self.list_plans(scope_empresa_id)
        plan = next((item for item in plans if item.codigo == code), None)
        if not plan:
            raise NotFoundException("Plan no encontrado.")
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(plan, field, value)
        if plan.codigo == "basico":
            plan.max_sucursales = 2
            plan.ia_generativa = False
            plan.capacidades = {
                **(plan.capacidades or {}), **MANDATORY_CAPABILITIES,
                "chatbot": False, "predicciones": False, "ia_generativa": False,
            }
        elif plan.codigo == "intermedio":
            plan.max_sucursales = 5
            plan.ia_generativa = False
            plan.capacidades = {
                **(plan.capacidades or {}), **MANDATORY_CAPABILITIES,
                "chatbot": True, "predicciones": False, "ia_generativa": False,
            }
        elif plan.codigo == "profesional":
            plan.max_sucursales = max(plan.max_sucursales or 10, 10)
            plan.ia_generativa = True
            plan.capacidades = {
                **(plan.capacidades or {}), **MANDATORY_CAPABILITIES,
                "chatbot": True, "predicciones": True, "ia_generativa": True,
            }
        self.db.commit()
        self.db.refresh(plan)
        BitacoraService.registrar(
            db=self.db,
            user=current_user,
            action=f"Actualizó el plan {plan.nombre} ({plan.codigo})",
            module="planes",
        )
        return plan

    def create_global_plan(self, payload: PlanCreate, current_user: User) -> PlanSuscripcion:
        self.resolve_admin_scope(current_user, None)
        self.seed_global_plans()
        exists = self.db.query(PlanSuscripcion.id).filter(
            PlanSuscripcion.scope_key == "global",
            PlanSuscripcion.codigo == payload.codigo,
        ).first()
        if exists:
            raise BadRequestException("Ya existe un plan global con ese código.")
        capabilities = {**MANDATORY_CAPABILITIES, **payload.capacidades}
        plan = PlanSuscripcion(
            empresa_id=None,
            scope_key="global",
            codigo=payload.codigo,
            nombre=payload.nombre,
            descripcion=payload.descripcion,
            precio_mensual=payload.precio_mensual,
            moneda=payload.moneda,
            max_sucursales=payload.max_sucursales,
            ia_generativa=payload.ia_generativa,
            beneficios=payload.beneficios,
            capacidades=capabilities,
            estilo=payload.estilo,
            active=payload.active,
        )
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        BitacoraService.registrar(
            db=self.db,
            user=current_user,
            action=f"Creó el plan global {plan.nombre} ({plan.codigo})",
            module="planes",
        )
        return plan

    def delete_global_plan(self, codigo: str, current_user: User) -> None:
        """Delete an unused custom global plan and its tenant copies.

        Contracts are immutable commercial evidence, so a plan referenced by any
        current or historical subscription must be hidden instead of deleted.
        """
        self.resolve_admin_scope(current_user, None)
        self.seed_global_plans()
        code = codigo.strip().lower()
        if code in PLAN_CODES:
            raise BadRequestException(
                "Los planes Básico, Intermedio y Profesional son planes base y no se pueden eliminar."
            )

        global_plan = self.db.query(PlanSuscripcion).filter(
            PlanSuscripcion.scope_key == "global",
            PlanSuscripcion.codigo == code,
        ).first()
        if not global_plan:
            raise NotFoundException("Plan global no encontrado.")

        plan_ids = [row[0] for row in self.db.query(PlanSuscripcion.id).filter(
            PlanSuscripcion.codigo == code
        ).all()]
        contracts = self.db.query(ContratacionPlan.id).filter(
            ContratacionPlan.plan_id.in_(plan_ids)
        ).count()
        if contracts:
            raise BadRequestException(
                "No se puede eliminar porque el plan tiene suscripciones o pagos registrados. "
                "Desactiva «Plan visible» para dejar de ofrecerlo sin perder el historial."
            )

        deleted_name = global_plan.nombre
        self.db.query(PlanSuscripcion).filter(
            PlanSuscripcion.codigo == code
        ).delete(synchronize_session=False)
        self.db.commit()
        BitacoraService.registrar(
            db=self.db,
            user=current_user,
            action=f"Eliminó el plan global {deleted_name} ({code})",
            module="planes",
        )

    def subscription_overview(self, current_user: User) -> dict:
        self.resolve_admin_scope(current_user, None)
        plans = self.list_plans(None)
        empresas = self.db.query(Empresa).order_by(Empresa.nombre.asc()).all()
        today = date.today()
        tenants = []
        counts = {plan.codigo: {"plan_codigo": plan.codigo, "plan_nombre": plan.nombre, "total": 0, "activas": 0} for plan in plans}
        for empresa in empresas:
            self.reconcile_tenant_access(empresa.id)
            contracts = self.db.query(ContratacionPlan).filter(
                ContratacionPlan.empresa_id == empresa.id
            ).order_by(ContratacionPlan.id.desc()).all()
            contract = next((item for item in contracts if item.estado == "activa"), None)
            contract = contract or next((item for item in contracts if item.estado == "pendiente"), None)
            contract = contract or (contracts[0] if contracts else None)
            days_remaining = None
            if contract and contract.fecha_fin:
                days_remaining = max((contract.fecha_fin - today).days, 0)
            row = {
                "empresa_id": empresa.id,
                "empresa_codigo": empresa.codigo,
                "empresa_nombre": empresa.nombre,
                "empresa_activa": empresa.active,
                "plan_codigo": contract.plan.codigo if contract and contract.plan else None,
                "plan_nombre": contract.plan.nombre if contract and contract.plan else None,
                "estado": contract.estado if contract else "sin_plan",
                "estado_pago": contract.estado_pago if contract else "sin_pago",
                "fecha_inicio": contract.fecha_inicio if contract else None,
                "fecha_fin": contract.fecha_fin if contract else None,
                "dias_restantes": days_remaining,
                "precio": contract.precio_snapshot if contract else None,
                "moneda": contract.moneda_snapshot if contract else None,
            }
            tenants.append(row)
            if row["plan_codigo"]:
                summary = counts.setdefault(row["plan_codigo"], {
                    "plan_codigo": row["plan_codigo"], "plan_nombre": row["plan_nombre"], "total": 0, "activas": 0,
                })
                summary["total"] += 1
                if row["estado"] == "activa" and row["estado_pago"] == "pagado" and (days_remaining is None or days_remaining >= 0):
                    summary["activas"] += 1
        return {"planes": list(counts.values()), "tenants": tenants}

    def reconcile_tenant_access(
        self, empresa_id: int, *, commit: bool = True
    ) -> tuple[Empresa, ContratacionPlan | None]:
        """Synchronize the tenant operational state with its paid subscription."""
        empresa = self.db.query(Empresa).filter(Empresa.id == empresa_id).first()
        if not empresa:
            raise NotFoundException("Empresa no encontrada.")

        # Legacy tenants created before subscriptions existed keep their current
        # state until the platform administrator assigns their first plan.
        has_contract = self.db.query(ContratacionPlan.id).filter(
            ContratacionPlan.empresa_id == empresa_id
        ).first()
        if not has_contract:
            return empresa, None

        today = date.today()
        changed = False
        expired_contracts = self.db.query(ContratacionPlan).filter(
            ContratacionPlan.empresa_id == empresa_id,
            ContratacionPlan.estado == "activa",
            ContratacionPlan.fecha_fin.is_not(None),
            ContratacionPlan.fecha_fin < today,
        ).all()
        for contract in expired_contracts:
            contract.estado = "vencida"
            changed = True

        valid_contract = (
            self.db.query(ContratacionPlan)
            .join(PlanSuscripcion, ContratacionPlan.plan_id == PlanSuscripcion.id)
            .filter(
                ContratacionPlan.empresa_id == empresa_id,
                ContratacionPlan.estado == "activa",
                ContratacionPlan.estado_pago == "pagado",
                ContratacionPlan.fecha_inicio <= today,
                (ContratacionPlan.fecha_fin.is_(None)) | (ContratacionPlan.fecha_fin >= today),
                PlanSuscripcion.active.is_(True),
            )
            .order_by(ContratacionPlan.id.desc())
            .first()
        )

        managed_reasons = {"subscription_expired", "payment_pending"}
        if valid_contract:
            if not empresa.active and empresa.inactive_reason in managed_reasons:
                empresa.active = True
                empresa.inactive_reason = None
                changed = True
        elif empresa.active or empresa.inactive_reason in managed_reasons:
            empresa.active = False
            empresa.inactive_reason = "subscription_expired"
            changed = True

        if changed and commit:
            self.db.commit()
            self.db.refresh(empresa)
        elif changed:
            self.db.flush()
        return empresa, valid_contract

    def contract(self, current_user: User, payload: ContratacionCreate | str) -> ContratacionPlan:
        # Internal fixtures and maintenance callers may pass the historical
        # string signature. Public API callers always go through the validated
        # schema and must confirm payment explicitly.
        if isinstance(payload, str):
            payload = ContratacionCreate(
                plan_codigo=payload,
                pago_confirmado=True,
                referencia_pago=f"INTERNAL-{uuid4().hex[:16].upper()}",
            )
        if current_user.role != "administrador":
            raise ForbiddenException(
                "Solo el administrador del tenant puede contratar o cambiar el plan de la empresa."
            )
        if current_user.empresa_id is None:
            raise BadRequestException("Tu cuenta no tiene una empresa asignada.")
        if not payload.pago_confirmado:
            raise BadRequestException(
                "Debes confirmar el pago de la renovación antes de activar el plan."
            )
        codigo = payload.plan_codigo
        plan = next(
            (item for item in self.list_plans(current_user.empresa_id, active_only=True) if item.codigo == codigo),
            None,
        )
        if not plan:
            raise NotFoundException("Plan no disponible.")
        self.db.query(ContratacionPlan).filter(
            ContratacionPlan.empresa_id == current_user.empresa_id,
            ContratacionPlan.estado == "activa",
        ).update({ContratacionPlan.estado: "reemplazada"}, synchronize_session=False)
        contract = ContratacionPlan(
            empresa_id=current_user.empresa_id,
            user_id=current_user.id,
            plan_id=plan.id,
            estado="activa",
            estado_pago="pagado",
            referencia_pago=payload.referencia_pago or f"SIM-{uuid4().hex[:16].upper()}",
            pagado_en=datetime.now(timezone.utc),
            precio_snapshot=plan.precio_mensual,
            moneda_snapshot=plan.moneda,
            fecha_inicio=date.today(),
            fecha_fin=date.today() + timedelta(days=30),
        )
        self.db.add(contract)
        empresa = self.db.query(Empresa).filter(Empresa.id == current_user.empresa_id).first()
        if empresa and empresa.inactive_reason in {"subscription_expired", "payment_pending"}:
            empresa.active = True
            empresa.inactive_reason = None
        self.db.commit()
        self.db.refresh(contract)
        BitacoraService.registrar(
            db=self.db, user=current_user,
            action=f"Contrató el plan {plan.nombre}", module="planes",
        )
        return contract

    def prepare_paypal_contract(self, current_user: User, plan_codigo: str) -> ContratacionPlan:
        """Create a pending contract without changing the tenant's current plan."""
        if current_user.role != "administrador" or current_user.is_platform_admin:
            raise ForbiddenException("Solo el administrador del tenant puede cambiar su plan.")
        if current_user.empresa_id is None:
            raise BadRequestException("Tu cuenta no tiene una empresa asignada.")
        plan = next(
            (item for item in self.list_plans(current_user.empresa_id, active_only=True)
             if item.codigo == plan_codigo),
            None,
        )
        if not plan:
            raise NotFoundException("Plan no disponible.")
        if plan.precio_mensual <= 0:
            raise BadRequestException("El administrador SaaS debe definir un precio mayor a cero para este plan.")
        if plan.moneda.upper() not in {"BOB", "USD", "EUR"}:
            raise BadRequestException(
                "La moneda configurada para este plan no es compatible. Usa BOB, USD o EUR."
            )
        self.db.query(ContratacionPlan).filter(
            ContratacionPlan.empresa_id == current_user.empresa_id,
            ContratacionPlan.estado == "pendiente",
            ContratacionPlan.estado_pago == "pendiente",
        ).update({ContratacionPlan.estado: "cancelada"}, synchronize_session=False)
        pending = ContratacionPlan(
            empresa_id=current_user.empresa_id,
            user_id=current_user.id,
            plan_id=plan.id,
            estado="pendiente",
            estado_pago="pendiente",
            precio_snapshot=plan.precio_mensual,
            moneda_snapshot=plan.moneda.upper(),
            fecha_inicio=date.today(),
            fecha_fin=date.today() + timedelta(days=30),
        )
        self.db.add(pending)
        self.db.flush()
        return pending

    def activate_paid_contract(
        self, current_user: User, pending: ContratacionPlan, paypal_capture_id: str
    ) -> ContratacionPlan:
        """Activate a plan only after PayPal confirms a completed capture."""
        if pending.empresa_id != current_user.empresa_id or pending.estado_pago != "pendiente":
            raise BadRequestException("La solicitud de pago no es válida o ya fue procesada.")
        self.db.query(ContratacionPlan).filter(
            ContratacionPlan.empresa_id == pending.empresa_id,
            ContratacionPlan.estado == "activa",
        ).update({ContratacionPlan.estado: "reemplazada"}, synchronize_session=False)
        pending.estado = "activa"
        pending.estado_pago = "pagado"
        pending.pagado_en = datetime.now(timezone.utc)
        pending.fecha_inicio = date.today()
        pending.fecha_fin = date.today() + timedelta(days=30)
        pending.referencia_pago = paypal_capture_id
        empresa = self.db.query(Empresa).filter(Empresa.id == pending.empresa_id).first()
        if empresa and empresa.inactive_reason in {"subscription_expired", "payment_pending"}:
            empresa.active = True
            empresa.inactive_reason = None
        self.db.commit()
        self.db.refresh(pending)
        BitacoraService.registrar(
            self.db, current_user,
            f"Pagó con PayPal y activó el plan {pending.plan.nombre}", "planes",
        )
        return pending

    def require_capability(self, current_user: User, capability: str) -> ContratacionPlan | None:
        """Require an active tenant subscription that enables a backend capability."""
        if current_user.is_platform_admin:
            return None
        if current_user.empresa_id is None:
            raise ForbiddenException("La cuenta no pertenece a una empresa contratante.")

        empresa, _ = self.reconcile_tenant_access(current_user.empresa_id)
        if not empresa.active:
            if empresa.inactive_reason == "subscription_expired":
                raise ForbiddenException(
                    "La suscripción venció. El tenant permanecerá inactivo hasta renovar y confirmar el pago."
                )
            raise ForbiddenException("La empresa se encuentra suspendida.")

        today = date.today()
        contract = (
            self.db.query(ContratacionPlan)
            .join(PlanSuscripcion, ContratacionPlan.plan_id == PlanSuscripcion.id)
            .filter(
                ContratacionPlan.empresa_id == current_user.empresa_id,
                ContratacionPlan.estado == "activa",
                ContratacionPlan.estado_pago == "pagado",
                ContratacionPlan.fecha_inicio <= today,
                (ContratacionPlan.fecha_fin.is_(None)) | (ContratacionPlan.fecha_fin >= today),
                PlanSuscripcion.active.is_(True),
            )
            .order_by(ContratacionPlan.id.desc())
            .first()
        )
        enabled = False
        if contract:
            if capability == "ia_generativa":
                enabled = bool(contract.plan.ia_generativa)
            else:
                enabled = bool((contract.plan.capacidades or {}).get(capability, False))
        if not enabled:
            raise ForbiddenException(
                "La suscripción vigente no incluye la capacidad "
                f"'{capability}'. Actualiza el plan para utilizarla."
            )
        return contract

    def require_branch_capacity(self, current_user: User) -> ContratacionPlan | None:
        """Enforce the active tenant plan branch quota before creating a branch."""
        if current_user.is_platform_admin:
            return None
        contract = self.require_capability(current_user, "productos")
        if contract is None or contract.plan.max_sucursales is None:
            return contract
        active_branches = self.db.query(Sucursal.id).filter(
            Sucursal.empresa_id == current_user.empresa_id,
            Sucursal.active.is_(True),
        ).count()
        if active_branches >= contract.plan.max_sucursales:
            raise ForbiddenException(
                f"El plan {contract.plan.nombre} permite hasta "
                f"{contract.plan.max_sucursales} sucursales activas. "
                "Cambia de plan o desactiva una sucursal para continuar."
            )
        return contract

    @staticmethod
    def serialize_contract(contract: ContratacionPlan) -> dict:
        return {
            "id": contract.id,
            "plan_codigo": contract.plan.codigo,
            "plan_nombre": contract.plan.nombre,
            "estado": contract.estado,
            "estado_pago": contract.estado_pago,
            "referencia_pago": contract.referencia_pago,
            "pagado_en": contract.pagado_en,
            "precio": contract.precio_snapshot,
            "moneda": contract.moneda_snapshot,
            "fecha_inicio": contract.fecha_inicio,
            "fecha_fin": contract.fecha_fin,
            "created_at": contract.created_at,
            "max_sucursales": contract.plan.max_sucursales if contract.plan else None,
            "capacidades": contract.plan.capacidades or {},
        }
