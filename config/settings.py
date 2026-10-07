import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY or enable local DJANGO_DEBUG=1")
    SECRET_KEY = "local-development-only-do-not-deploy"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
INSTALLED_APPS = ["config.apps.SuperuserAdminConfig", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "rest_framework", "rest_framework.authtoken", "drf_spectacular", "mall"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
if os.getenv("POSTGRES_DB"):
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": os.environ["POSTGRES_DB"], "USER": os.getenv("POSTGRES_USER", "partsmall"), "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""), "HOST": os.getenv("POSTGRES_HOST", "localhost"), "PORT": os.getenv("POSTGRES_PORT", "5432")}}
AUTH_USER_MODEL = "mall.User"
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"}, {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}, {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"}, {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"}]
LANGUAGE_CODE = "es"
LANGUAGES = [("es", "Español")]
USE_I18N = True
TIME_ZONE = "America/Panama"
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = os.getenv("STATIC_ROOT", str(BASE_DIR / "staticfiles"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Catalog images are public product media; credentials stay on the API server.
DO_SPACES_KEY = os.getenv('DO_SPACES_KEY', '')
DO_SPACES_SECRET = os.getenv('DO_SPACES_SECRET', '')
DO_SPACES_BUCKET = os.getenv('DO_SPACES_BUCKET', '')
DO_SPACES_REGION = os.getenv('DO_SPACES_REGION', 'nyc3')
DO_SPACES_ENDPOINT = os.getenv('DO_SPACES_ENDPOINT') or f'https://{DO_SPACES_REGION}.digitaloceanspaces.com'
DO_SPACES_CDN_ENABLED = os.getenv('DO_SPACES_CDN_ENABLED', 'true').lower() in ('true', '1', 'yes')
DO_SPACES_CDN_ENDPOINT = os.getenv('DO_SPACES_CDN_ENDPOINT') or f'https://{DO_SPACES_BUCKET}.{DO_SPACES_REGION}.cdn.digitaloceanspaces.com'
DO_SPACES_MEDIA_PREFIX = os.getenv('DO_SPACES_MEDIA_PREFIX', 'motionpartes/media').strip('/')
MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = '/media/'
CATALOG_IMAGE_MAX_BYTES = 10 * 1024 * 1024
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'catalog_media': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
}
if any((DO_SPACES_KEY, DO_SPACES_SECRET, DO_SPACES_BUCKET)):
    if not all((DO_SPACES_KEY, DO_SPACES_SECRET, DO_SPACES_BUCKET, DO_SPACES_MEDIA_PREFIX)):
        raise ImproperlyConfigured('Configure DO_SPACES_KEY, DO_SPACES_SECRET, DO_SPACES_BUCKET and DO_SPACES_MEDIA_PREFIX together.')
    from urllib.parse import urlparse
    cdn = urlparse(DO_SPACES_CDN_ENDPOINT)
    if cdn.scheme != 'https' or not cdn.netloc or cdn.path.strip('/'):
        raise ImproperlyConfigured('DO_SPACES_CDN_ENDPOINT must be an HTTPS origin without a path.')
    STORAGES['catalog_media'] = {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'access_key': DO_SPACES_KEY, 'secret_key': DO_SPACES_SECRET,
            'bucket_name': DO_SPACES_BUCKET, 'region_name': DO_SPACES_REGION,
            'endpoint_url': DO_SPACES_ENDPOINT,
            'custom_domain': cdn.netloc if DO_SPACES_CDN_ENABLED else None,
            'default_acl': 'public-read', 'querystring_auth': False,
            'location': DO_SPACES_MEDIA_PREFIX, 'file_overwrite': False,
            'signature_version': 's3v4', 'addressing_style': 'virtual',
            'object_parameters': {'CacheControl': 'public, max-age=31536000, immutable'},
        },
    }
REST_FRAMEWORK = {"DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication", "rest_framework.authentication.SessionAuthentication"], "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"], "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema", "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination", "PAGE_SIZE": 50, "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"], "DEFAULT_THROTTLE_RATES": {"anon": "20/min"}}
SPECTACULAR_SETTINGS = {"TITLE": "MotionPartes API", "VERSION": "0.1.0", "COMPONENT_SPLIT_REQUEST": True, "ENUM_NAME_OVERRIDES": {
    # Shared choice sets get one stable name; values and labels must match the fields exactly.
    "LedgerDirectionEnum": [("credit", "Crédito"), ("debit", "Débito")],
    "SupplierItemSourceEnum": [("upload", "Carga manual"), ("apiag", "apiag-cloud")], "InventorySourceEnum": ["upload", "apiag"],
    "ReferenceKindEnum": ["alias", "oem", "manufacturer"],
    "DealActionEnum": ["quote", "accept", "request_adjustment", "return_quote"], "MatchingReviewActionEnum": ["approve", "dismiss"],
    "MatchingStateEnum": ["running", "retrying", "waiting_import", "queued", "completed", "idle"],
    # Private pricing (supplier-only). MembershipPermissionEnum carries the Membership.permission labels.
    "CurrencyEnum": ["USD", "PAB"], "MembershipPermissionEnum": [("owner", "Propietario"), ("manager", "Administrador"), ("staff", "Empleado")],
    "SupplierPolicyEnum": ["confirm", "block"], "PriceSourceEnum": ["engine", "previous", "manual", "none"],
    "QuantitySourceEnum": ["requested", "available", "previous", "manual", "assistant"], "PricingRuleScopeEnum": ["all", "client"],
    "PricingRuleTargetEnum": ["all", "line", "brand", "item"], "PricingRuleKindEnum": ["discount", "net_price"],
    "ExceptionSeverityEnum": ["block", "confirm", "info"], "QuoteDraftStatusEnum": ["editing", "review_requested"],
    "QuoteExceptionCodeEnum": ["quantity_missing", "price_missing", "all_zero", "draft_outdated", "offered_gt_available", "offered_gt_requested",
                               "identity_changed", "zero_price", "below_floor", "stale_price", "reduced_to_stock", "zero_offered", "manual_price",
                               "differs_from_list", "no_list_price", "currency_parity", "currency_mismatch", "fallback_list", "rule_conflict", "no_profile",
                               "client_price_request", "assistant_unmatched"],
    "AcknowledgeableExceptionEnum": ["offered_gt_available", "offered_gt_requested", "identity_changed", "zero_price", "below_floor", "stale_price"],
    "AuditPriceSourceEnum": ["engine", "previous", "manual", "none", "unspecified"], "AcceptCheckResultEnum": ["ok", "blocked", "accepted_with_shortfall"],
    "PriceChangeKindEnum": ["list_price", "floor_price", "discount_group"], "PriceImportActionEnum": ["create", "update", "delete"],
    "PriceImportStatusEnum": ["ready", "review", "importing", "completed"],
    # Quotation assistant (supplier-only). Proposal kinds never include a price.
    "AssistantRunStatusEnum": ["claimed", "completed", "failed"],
    "AssistantProposalKindEnum": ["quantity_change", "remove_line", "line_note", "terms", "price_request", "unmatched", "question"],
    "AssistantDecisionEnum": ["applied", "dismissed"], "AssistantActionEnum": ["apply", "dismiss"],
    # Code suffix table (OEM finder). Labels must match CatalogCodeSuffix.status.
    "CatalogSuffixStatusEnum": [("seed_confirmed", "Confirmado en la semilla"), ("needs_owner_confirmation", "Por confirmar"),
                                ("needs_owner_label", "Por etiquetar"), ("owner_confirmed", "Confirmado por el propietario")],
    # OEM finder runs and auto-apply (superuser-only).
    "OEMRunModeEnum": ["dry_run", "apply_auto", "ai"], "OEMRunStatusEnum": ["running", "completed", "failed"],
    "OEMApplyTierEnum": ["AUTO_FLAG_CURRENT", "AUTO_RENAME_BASE", "STRONG_PENDING_OWNER_TAGS"],
    "OEMApplyMethodEnum": ["flag_current", "rename_base", "rename_confirmed_tags"], "OEMHaltActionEnum": ["halt", "resume"],
    # Revisión OEM queue (superuser-only). Tiers and statuses must match mall.oem_finder.REVIEW_TIERS and OEMReviewCase.status.
    "OEMReviewTierEnum": ["STRONG_PENDING_OWNER_TAGS", "CURRENT_REVIEW", "PROBABLE_BASE", "CURRENT_LIKELY_OEM", "LOCAL_XREF", "MULTI_OEM",
                          "CONFLICT", "VARIANT_REVIEW", "UNKNOWN_SUFFIX", "WEAK", "NEEDS_AI"],
    "OEMReviewStatusEnum": ["review", "applied", "dismissed", "resolved"], "OEMReviewMethodEnum": ["flag", "rename"],
    "OEMReviewActionEnum": ["approve", "choose_oem", "send_to_merge", "dismiss", "reopen"],
    "OEMDismissReasonEnum": ["not_oem", "other_part", "keep_code", "data_error", "other"], "OEMReviewOutcomeEnum": ["applied", "conflict", "skipped"],
    "OEMBulkStepActionEnum": ["continue", "stop"]}}
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') or os.getenv('GEM_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')


def env_usd(name, default):
    from decimal import Decimal, InvalidOperation
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation:
        raise ImproperlyConfigured(f'{name} must be a decimal amount in USD.')
    if not value.is_finite() or value < 0:
        raise ImproperlyConfigured(f'{name} must be zero or a positive amount in USD.')
    return value


# AI quotation assistant (supplier opt-in). Ships dark: with QUOTE_ASSISTANT_MONTHLY_USD at 0 it never calls the provider
# (40 of the 200 platform budget is the recommended cap). AI_MONTHLY_BUDGET_USD caps every feature that records AI usage.
QUOTE_ASSISTANT_MONTHLY_USD = env_usd('QUOTE_ASSISTANT_MONTHLY_USD', '0')
AI_MONTHLY_BUDGET_USD = env_usd('AI_MONTHLY_BUDGET_USD', '200')
QUOTE_ASSISTANT_RUNS_PER_REVISION = max(1, int(os.getenv('QUOTE_ASSISTANT_RUNS_PER_REVISION', '5')))
QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT = max(1, int(os.getenv('QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT', '30')))
# PLACEHOLDER token prices in USD per million tokens, used only to estimate and record AI spend against the caps above. They are NOT
# Google's prices: set both from Google's current Gemini price sheet for GEMINI_MODEL. Thinking tokens are counted at the output rate.
GEMINI_INPUT_USD_PER_MTOK = env_usd('GEMINI_INPUT_USD_PER_MTOK', '1.00')
GEMINI_OUTPUT_USD_PER_MTOK = env_usd('GEMINI_OUTPUT_USD_PER_MTOK', '5.00')
# PLACEHOLDER fee per Google Search grounded request (the OEM lookup), added to its token cost. Set it from Google's price sheet too.
GEMINI_GROUNDING_USD_PER_CALL = env_usd('GEMINI_GROUNDING_USD_PER_CALL', '0.035')
# Every quotation is published from the supplier-reviewed server draft (draft_version). 0 re-opens the draftless legacy API path.
QUOTES_REQUIRE_DRAFT = os.getenv('QUOTES_REQUIRE_DRAFT', '1').lower() in ('true', '1', 'yes')
# OEM finder dry run as a matching worker stage after catalog grouping (off by default; python -m mall.oem_finder runs it by hand).
OEM_FINDER_AUTO_STAGE = os.getenv('OEM_FINDER_AUTO_STAGE', '0').lower() in ('true', '1', 'yes')
CATALOG_LOW_STOCK_THRESHOLD = max(1, int(os.getenv('CATALOG_LOW_STOCK_THRESHOLD', '5')))
CATALOG_HIGH_STOCK_THRESHOLD = int(os.getenv('CATALOG_HIGH_STOCK_THRESHOLD', '20'))
if CATALOG_HIGH_STOCK_THRESHOLD <= CATALOG_LOW_STOCK_THRESHOLD:
    raise ImproperlyConfigured(f'CATALOG_HIGH_STOCK_THRESHOLD ({CATALOG_HIGH_STOCK_THRESHOLD}) must be greater than CATALOG_LOW_STOCK_THRESHOLD ({CATALOG_LOW_STOCK_THRESHOLD}).')
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
