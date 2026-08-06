"""Central health component registry — labels, criticality, alert policy."""

from __future__ import annotations

from dataclasses import dataclass

from app.health.models import AlertPolicy, Criticality, ProductArea, RecoveryPolicy

SAFE_GENERIC_LABEL_UK = "Невідомий компонент"
SAFE_GENERIC_AREA_UK = "Система"


@dataclass(frozen=True)
class HealthComponentSpec:
    component_id: str
    label_uk: str
    product_area: ProductArea
    criticality: Criticality
    capability_uk: str
    impact_degraded_uk: str
    impact_down_uk: str
    fail_safe_uk: str
    admin_action_down_uk: str
    admin_action_degraded_uk: str
    alert_policy: AlertPolicy
    recovery_policy: RecoveryPolicy
    reason_codes: frozenset[str]
    ui_group: str
    product_area_uk: str


def _spec(
    component_id: str,
    *,
    label_uk: str,
    product_area: ProductArea,
    product_area_uk: str,
    criticality: Criticality,
    capability_uk: str,
    impact_degraded_uk: str,
    impact_down_uk: str,
    fail_safe_uk: str,
    admin_action_down_uk: str,
    admin_action_degraded_uk: str,
    alert_policy: AlertPolicy,
    recovery_policy: RecoveryPolicy,
    reason_codes: frozenset[str],
    ui_group: str,
) -> HealthComponentSpec:
    return HealthComponentSpec(
        component_id=component_id,
        label_uk=label_uk,
        product_area=product_area,
        product_area_uk=product_area_uk,
        criticality=criticality,
        capability_uk=capability_uk,
        impact_degraded_uk=impact_degraded_uk,
        impact_down_uk=impact_down_uk,
        fail_safe_uk=fail_safe_uk,
        admin_action_down_uk=admin_action_down_uk,
        admin_action_degraded_uk=admin_action_degraded_uk,
        alert_policy=alert_policy,
        recovery_policy=recovery_policy,
        reason_codes=reason_codes,
        ui_group=ui_group,
    )


_REGISTRY: dict[str, HealthComponentSpec] = {}


def _register(spec: HealthComponentSpec) -> None:
    _REGISTRY[spec.component_id] = spec


def get_component_spec(component_id: str) -> HealthComponentSpec | None:
    return _REGISTRY.get(component_id)


def require_component_spec(component_id: str) -> HealthComponentSpec | None:
    return get_component_spec(component_id)


def label_uk_for(component_id: str) -> str:
    spec = get_component_spec(component_id)
    return spec.label_uk if spec is not None else SAFE_GENERIC_LABEL_UK


def product_area_uk_for(component_id: str) -> str:
    spec = get_component_spec(component_id)
    return spec.product_area_uk if spec is not None else SAFE_GENERIC_AREA_UK


def ui_group_for(component_id: str) -> str:
    spec = get_component_spec(component_id)
    return spec.ui_group if spec is not None else "Core Runtime"


def all_registered_ids() -> frozenset[str]:
    return frozenset(_REGISTRY)


def all_specs() -> tuple[HealthComponentSpec, ...]:
    return tuple(_REGISTRY[k] for k in sorted(_REGISTRY))


# --- Register every currently emitted probe / alert component ---

_register(
    _spec(
        "telegram",
        label_uk="Telegram-опитування",
        product_area="runtime",
        product_area_uk="Середовище виконання",
        criticality="hard_critical",
        capability_uk="Отримання оновлень Telegram і обробка команд",
        impact_degraded_uk="Збільшена затримка відповідей; меню ще доступні",
        impact_down_uk="Бот не отримує оновлення Telegram надійно",
        fail_safe_uk="Нові команди можуть не оброблятися; торгівля не запускається",
        admin_action_down_uk="Перевірте, що працює лише один логічний poller; усуньте getUpdates conflict",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="immediate_hard_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset(
            {"poller_conflict", "poller_stopped", "startup_timeout", "runtime_fault"}
        ),
        ui_group="Core Runtime",
    )
)
_register(
    _spec(
        "scheduler",
        label_uk="Планувальник завдань",
        product_area="runtime",
        product_area_uk="Середовище виконання",
        criticality="feature_critical",
        capability_uk="Фонові JobQueue завдання (сканери, оновлення, health tick)",
        impact_degraded_uk="Окремі некритичні завдання затримуються",
        impact_down_uk="Критичні оновлення/сканери не виконуються за розкладом",
        fail_safe_uk="Пропущені цикли не публікують застарілі сигнали",
        admin_action_down_uk="Перевірте журнали JobQueue і критичні job executions",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"job_stale", "job_failed", "queue_unavailable", "critical_job_down"}),
        ui_group="Core Runtime",
    )
)
_register(
    _spec(
        "sqlite",
        label_uk="База даних SQLite",
        product_area="storage",
        product_area_uk="Сховище",
        criticality="hard_critical",
        capability_uk="Читання/запис робочої бази",
        impact_degraded_uk="Обмежена продуктивність сховища",
        impact_down_uk="Запис або цілісність БД недоступні",
        fail_safe_uk="Операції запису блокуються; деструктивне відновлення не виконується автоматично",
        admin_action_down_uk="Перевірте доступність live_database і цілісність SQLite",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="immediate_hard_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"db_unavailable", "integrity_failed", "write_failed"}),
        ui_group="Storage",
    )
)
_register(
    _spec(
        "infrastructure",
        label_uk="Інфраструктура сховища",
        product_area="storage",
        product_area_uk="Сховище",
        criticality="feature_critical",
        capability_uk="Диск, журнали, метрики сховища",
        impact_degraded_uk="Затримки cleanup/retention при безпечних записах",
        impact_down_uk="Інфраструктура сховища в критичному стані",
        fail_safe_uk="Автоматичне деструктивне очищення вимкнено",
        admin_action_down_uk="Перегляньте Storage у System Health; підтвердіть backup перед prune",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"disk_pressure", "storage_critical", "storage_degraded"}),
        ui_group="Storage",
    )
)
_register(
    _spec(
        "storage",
        label_uk="Сповіщення сховища",
        product_area="storage",
        product_area_uk="Сховище",
        criticality="feature_critical",
        capability_uk="Переходи рівня сховища, backup і retention gate",
        impact_degraded_uk="Сховище працює з попередженнями",
        impact_down_uk="Критичний рівень сховища або збій backup/gate",
        fail_safe_uk="Prune/cleanup лишаються заблокованими без підтвердження",
        admin_action_down_uk="Відкрийте Storage у System Health",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset(
            {
                "storage_level",
                "backup_failed",
                "backup_stale",
                "retention_gate",
                "cleanup_failed",
                "prune_failed",
            }
        ),
        ui_group="Storage",
    )
)
_register(
    _spec(
        "weather",
        label_uk="Weather Intelligence",
        product_area="weather",
        product_area_uk="Погода",
        criticality="feature_critical",
        capability_uk="Прогнози та сканування погодних ринків",
        impact_degraded_uk="Частина опційних провайдерів недоступна; основне джерело працює",
        impact_down_uk="Первинні погодні дані недоступні",
        fail_safe_uk="Застарілі прогнози не показуються як актуальні",
        admin_action_down_uk="Перевірте погодні провайдери та FORECAST_MIN_PROVIDERS",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"primary_unavailable", "providers_low", "stale_forecast"}),
        ui_group="Weather",
    )
)
_register(
    _spec(
        "copy_intel",
        label_uk="Copy Intelligence",
        product_area="copy",
        product_area_uk="Copy Intelligence",
        criticality="feature_critical",
        capability_uk="Читання гаманців і paper-алерти Copy",
        impact_degraded_uk="Частина гаманців з incomplete_book лишається fail-closed",
        impact_down_uk="Немає надійної обробки Copy",
        fail_safe_uk="Гаманці з неповними книгами не генерують алерти",
        admin_action_down_uk="Перевірте poll freshness і baselines у Copy",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset(
            {"poll_stale", "baselines_incomplete", "no_trustworthy_processing", "api_errors"}
        ),
        ui_group="Copy Intelligence",
    )
)
_register(
    _spec(
        "copy_readonly",
        label_uk="Copy режим лише читання",
        product_area="copy",
        product_area_uk="Copy Intelligence",
        criticality="hard_critical",
        capability_uk="Інваріант read-only для Copy Intelligence",
        impact_degraded_uk="Не застосовується",
        impact_down_uk="Порушено read-only safety для Copy",
        fail_safe_uk="Торгівля/підпис ордерів заборонені; інцидент критичний",
        admin_action_down_uk="Негайно відновіть COPY_INTEL_READ_ONLY=true і перевірте конфіг",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="immediate_hard_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"readonly_violation"}),
        ui_group="Copy Intelligence",
    )
)
_register(
    _spec(
        "crypto_intel",
        label_uk="Crypto Intelligence",
        product_area="crypto",
        product_area_uk="Crypto Intelligence",
        criticality="feature_critical",
        capability_uk="Публічні ринкові дані та paper-хуки Crypto",
        impact_degraded_uk="Одна біржа недоступна; інші майданчики працюють",
        impact_down_uk="Немає придатних майданчиків/книг для executable scanning",
        fail_safe_uk="Неперевірені ціна/ліквідність/slippage блокують публікацію",
        admin_action_down_uk="Перевірте публічні адаптери бірж",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"venue_down", "no_usable_venues", "book_unusable"}),
        ui_group="Crypto Intelligence",
    )
)
_register(
    _spec(
        "crypto_readonly",
        label_uk="Crypto режим лише читання",
        product_area="crypto",
        product_area_uk="Crypto Intelligence",
        criticality="hard_critical",
        capability_uk="Інваріант read-only для Crypto Intelligence",
        impact_degraded_uk="Не застосовується",
        impact_down_uk="Порушено read-only safety для Crypto",
        fail_safe_uk="Ордери/виводи/ключі гаманця заборонені",
        admin_action_down_uk="Негайно відновіть CRYPTO_INTEL_READ_ONLY=true",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="immediate_hard_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"readonly_violation"}),
        ui_group="Crypto Intelligence",
    )
)
_register(
    _spec(
        "market_opportunity",
        label_uk="Market Opportunity Intelligence",
        product_area="market_opportunities",
        product_area_uk="Market Opportunities",
        criticality="feature_critical",
        capability_uk="Paper WTI детектори на EIA та опційний FRED",
        impact_degraded_uk="Опційний FRED недоступний; EIA може бути здоровим",
        impact_down_uk="Первинне джерело EIA RWTC недоступне або застаріле",
        fail_safe_uk="Застарілі ціни блокують нові WTI можливості",
        admin_action_down_uk="Перевірте EIA RWTC refresh і runtime MOI",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"eia_price_down", "fred_optional", "warming", "stale_price"}),
        ui_group="Market Opportunities",
    )
)
_register(
    _spec(
        "opportunity_discovery",
        label_uk="Polymarket discovery",
        product_area="polymarket",
        product_area_uk="Polymarket",
        criticality="feature_critical",
        capability_uk="Виявлення активних Polymarket ринків",
        impact_degraded_uk="Зменшене покриття валідних активних ринків",
        impact_down_uk="Первинне discovery недоступне",
        fail_safe_uk="Завершені/застарілі ринки лишаються виключеними",
        admin_action_down_uk="Перевірте Gamma/discovery статус Opportunity Scanner",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"discovery_unavailable", "discovery_incomplete", "reduced_coverage"}),
        ui_group="Polymarket",
    )
)
_register(
    _spec(
        "opportunity_books",
        label_uk="Книги ордерів Polymarket",
        product_area="polymarket",
        product_area_uk="Polymarket",
        criticality="feature_critical",
        capability_uk="Валідні executable order books для сканера можливостей",
        impact_degraded_uk="Зменшене, але ще придатне покриття книг",
        impact_down_uk="Немає валідних executable книг після спроб читання",
        fail_safe_uk="Неповні/неперевірені книги блокують публікацію спостережень",
        admin_action_down_uk="Перевірте CLOB/book клієнт і бюджет книг на цикл",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"reduced_book_coverage", "zero_valid_books", "book_budget_skipped"}),
        ui_group="Polymarket",
    )
)
_register(
    _spec(
        "opportunity_history",
        label_uk="Історія цін Polymarket",
        product_area="polymarket",
        product_area_uk="Polymarket",
        criticality="optional",
        capability_uk="Історичні ряди для руху ціни",
        impact_degraded_uk="Часткові збої історії без блокування безпечної поточної роботи",
        impact_down_uk="Історія недоступна для оцінки руху",
        fail_safe_uk="Спостереження без свіжої історії не публікуються",
        admin_action_down_uk="Перевірте history client",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="never",
        recovery_policy="none",
        reason_codes=frozenset({"history_lag", "history_ratio_high", "history_unavailable"}),
        ui_group="Polymarket",
    )
)
_register(
    _spec(
        "opportunity_scheduler",
        label_uk="Розклад Opportunity Scanner",
        product_area="polymarket",
        product_area_uk="Polymarket",
        criticality="feature_critical",
        capability_uk="Цикли Opportunity Scanner за розкладом",
        impact_degraded_uk="Пропущені overlap-цикли в межах допуску",
        impact_down_uk="Критичний цикл сканера неодноразово не виконується",
        fail_safe_uk="Idle/outcome jobs не стирають інші діагностики",
        admin_action_down_uk="Перевірте opportunity scan job registrations",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"skipped_overlaps", "scanner_stale", "scheduler_down"}),
        ui_group="Polymarket",
    )
)
_register(
    _spec(
        "opportunity_storage",
        label_uk="Сховище Opportunity Scanner",
        product_area="storage",
        product_area_uk="Сховище",
        criticality="hard_critical",
        capability_uk="Персистентність paper можливостей і runtime",
        impact_degraded_uk="Обмежена персистентність scanner state",
        impact_down_uk="Сховище opportunity недоступне",
        fail_safe_uk="Цикли без сховища не публікують",
        admin_action_down_uk="Перевірте opportunity schema і запис БД",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="immediate_hard_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"storage_unavailable", "schema_fault"}),
        ui_group="Storage",
    )
)
_register(
    _spec(
        "opportunity_delivery",
        label_uk="Доставка Opportunity Scanner",
        product_area="delivery",
        product_area_uk="Доставка",
        criticality="feature_critical",
        capability_uk="Доставка paper-алертів Opportunity Scanner",
        impact_degraded_uk="Черга доставки під навантаженням",
        impact_down_uk="Доставка увімкнена, але підтверджено недоступна",
        fail_safe_uk="Вимкнені auto-alerts = DISABLED, не збій",
        admin_action_down_uk="Перевірте delivery worker і чергу",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="on_confirmed_down",
        recovery_policy="once_after_notified_down",
        reason_codes=frozenset({"delivery_down", "dead_letter", "delivery_disabled"}),
        ui_group="Delivery",
    )
)
_register(
    _spec(
        "opportunity_baseline",
        label_uk="Baseline Opportunity Scanner",
        product_area="polymarket",
        product_area_uk="Polymarket",
        criticality="optional",
        capability_uk="Початковий baseline циклів сканера",
        impact_degraded_uk="Baseline ще не завершено",
        impact_down_uk="Baseline зірвано",
        fail_safe_uk="Алерти до завершення baseline не надсилаються",
        admin_action_down_uk="Дочекайтеся завершення baseline",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="never",
        recovery_policy="none",
        reason_codes=frozenset({"baselining", "baseline_incomplete"}),
        ui_group="Polymarket",
    )
)
_register(
    _spec(
        "health_monitor",
        label_uk="Монітор здоров’я",
        product_area="runtime",
        product_area_uk="Середовище виконання",
        criticality="optional",
        capability_uk="Періодичні health probes",
        impact_degraded_uk="Не застосовується",
        impact_down_uk="Не застосовується",
        fail_safe_uk="Вимкнений монітор = DISABLED",
        admin_action_down_uk="Втручання не потрібне",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="never",
        recovery_policy="none",
        reason_codes=frozenset({"monitor_disabled"}),
        ui_group="Core Runtime",
    )
)

_register(
    _spec(
        "overall",
        label_uk="Загальний стан системи",
        product_area="runtime",
        product_area_uk="Середовище виконання",
        criticality="feature_critical",
        capability_uk="Агрегований стан усіх підсистем",
        impact_degraded_uk="Одна або кілька можливостей обмежені",
        impact_down_uk="Критична можливість недоступна",
        fail_safe_uk="Перевага віддається сповіщенням конкретних можливостей",
        admin_action_down_uk="Відкрийте System Health і перегляньте критичні розділи",
        admin_action_degraded_uk="Втручання не потрібне",
        alert_policy="never",
        recovery_policy="none",
        reason_codes=frozenset({"status_degraded", "status_down"}),
        ui_group="Core Runtime",
    )
)

UI_GROUP_ORDER: tuple[str, ...] = (
    "Core Runtime",
    "Storage",
    "Polymarket",
    "Weather",
    "Copy Intelligence",
    "Crypto Intelligence",
    "Market Opportunities",
    "Delivery",
    "External Sources",
)

STATUS_LABEL_UK: dict[str, str] = {
    "HEALTHY": "Працює",
    "DEGRADED": "Обмежено",
    "DOWN": "Недоступно",
    "DISABLED": "Вимкнено",
    "WARMING_UP": "Прогрів",
    "UNKNOWN": "Невідомо",
}
