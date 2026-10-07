from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.throttling import AnonRateThrottle
from mall import management, views
from mall.technical_api import (PartTypeList, PartTypeDetail, TemplateDetail, ApplicationList, ApplicationDetail,
                               ItemTechnicalDetail, CatalogTechnicalDetail)
from mall.matching_views import MatchingOverview, MatchingReview
from mall.analytics import AnalyticsDashboard, UsageEvents
from mall.wishlist import WishlistList, WishlistState, WishlistDetail
from mall.catalog_import import CatalogImport, CatalogImportJobView
from mall.catalog_classification import CatalogClassification
from mall.catalog_assistant import CatalogAssistantList, CatalogAssistantDetail
from mall.catalog_import_issues import CatalogImportIssueList, CatalogImportIssueDetail
from mall.catalog_grouping import CatalogGroupingMerge
from mall.oem_lookup import CatalogOEMLookup
from mall.catalog_suffix_views import CompanySuffixes, SuffixDetail, SuffixList
from mall.oem_finder_views import OEMApplyHaltView, OEMRunDetail, OEMRunList, OEMRunRevert, OEMRunSpotCheck
from mall.catalog_media import CatalogImages, CatalogImageDetail, CatalogImageOrder
from mall.catalog_grouping_suggestions import CatalogGroupingList, CatalogGroupingClassify
from mall.supplier_import import SupplierInventoryImport, SupplierInventoryImportTemplate, SupplierInventoryImportJobView, SupplierInventoryImportErrors
from mall.request_views import AccountRequests, ClientSentRequests, ClientSentRequestDetail, ClientPartRequestState, SupplierRequestDetail, SupplierRequestReview
from mall.deal_views import DealActions, DealMessages
from mall.pricing_views import (ClientProfileView, PriceHistory, PriceListDetail, PriceListsView, PricesExport, PricesView, PricingClients, PricingHistory,
                               PricingRuleArchiveView, PricingRuleDetail, PricingRulesView, PricingSettingsView, PricingSimulate)
from mall.quote_drafts import QuotationTrace, QuoteAssistantDecisions, QuoteAssistantView, QuoteDraftDiscard, QuoteDraftReprice, QuoteDraftView
from mall.price_import import PriceImport, PriceImportErrors, PriceImportJobView, PriceImportTemplate
from .health import health

class LoginView(ObtainAuthToken):
    throttle_classes = [AnonRateThrottle]

urlpatterns = [
    path('health/', health),
    path('admin/', admin.site.urls),
    path('api/v1/auth/signup/', views.SignupView.as_view()),
    path('api/v1/auth/login/', LoginView.as_view()),
    path('api/v1/auth/logout/', views.LogoutView.as_view()),
    path('api/v1/auth/me/', management.ProfileView.as_view()),
    path('api/v1/analytics/events/', UsageEvents.as_view()),
    path('api/v1/management/analytics/', AnalyticsDashboard.as_view()),
    path('api/v1/management/catalog/', management.CatalogList.as_view()),
    path('api/v1/management/part-types/', PartTypeList.as_view()),
    path('api/v1/management/part-types/<uuid:pk>/', PartTypeDetail.as_view()),
    path('api/v1/management/part-types/<uuid:pk>/template/', TemplateDetail.as_view()),
    path('api/v1/management/applications/', ApplicationList.as_view()),
    path('api/v1/management/applications/<uuid:pk>/', ApplicationDetail.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/technical/', ItemTechnicalDetail.as_view()),
    path('api/v1/management/catalog/assistant/', CatalogAssistantList.as_view()),
    path('api/v1/management/catalog/assistant/<uuid:pk>/', CatalogAssistantDetail.as_view()),
    path('api/v1/management/catalog/grouping/', CatalogGroupingList.as_view()),
    path('api/v1/management/catalog/grouping/classify/', CatalogGroupingClassify.as_view()),
    path('api/v1/management/catalog/grouping/merge/', CatalogGroupingMerge.as_view()),
    path('api/v1/management/catalog/import/', CatalogImport.as_view()),
    path('api/v1/management/catalog/import/issues/', CatalogImportIssueList.as_view()),
    path('api/v1/management/catalog/import/issues/<uuid:pk>/', CatalogImportIssueDetail.as_view()),
    path('api/v1/management/catalog/import/jobs/<uuid:pk>/', CatalogImportJobView.as_view()),
    path('api/v1/management/catalog/import/jobs/<uuid:pk>/classification/', CatalogClassification.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/oem-lookup/', CatalogOEMLookup.as_view()),
    path('api/v1/management/catalog/suffixes/', SuffixList.as_view()),
    path('api/v1/management/catalog/suffixes/<path:token>/', SuffixDetail.as_view()),
    path('api/v1/management/catalog/company-suffixes/', CompanySuffixes.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/', management.CatalogDetail.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/images/', CatalogImages.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/images/order/', CatalogImageOrder.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/images/<uuid:image_id>/', CatalogImageDetail.as_view()),
    path('api/v1/management/oem-finder/runs/', OEMRunList.as_view()),
    path('api/v1/management/oem-finder/runs/<int:pk>/', OEMRunDetail.as_view()),
    path('api/v1/management/oem-finder/runs/<int:pk>/spot-check/', OEMRunSpotCheck.as_view()),
    path('api/v1/management/oem-finder/runs/<int:pk>/revert/', OEMRunRevert.as_view()),
    path('api/v1/management/oem-finder/halt/', OEMApplyHaltView.as_view()),
    path('api/v1/management/matching/', MatchingOverview.as_view()),
    path('api/v1/management/matching/<uuid:pk>/', MatchingReview.as_view()),
    path('api/v1/management/inventory/', management.InventoryList.as_view()),
    path('api/v1/management/inventory/<uuid:pk>/', management.InventoryDetail.as_view()),
    path('api/v1/management/alternates/', management.AlternateList.as_view()),
    path('api/v1/management/alternates/<int:pk>/', management.AlternateDetail.as_view()),
    path('api/v1/management/accounts/', management.AccountList.as_view()),
    path('api/v1/management/accounts/<uuid:pk>/', management.AccountDetail.as_view()),
    path('api/v1/management/users/', management.UserList.as_view()),
    path('api/v1/management/users/<int:pk>/', management.UserDetail.as_view()),
    path('api/v1/management/roles/', management.RoleList.as_view()),
    path('api/v1/management/memberships/', management.MembershipCreate.as_view()),
    path('api/v1/management/memberships/<int:pk>/', management.MembershipDetail.as_view()),
    path('api/v1/management/invitations/', management.InvitationList.as_view()),
    path('api/v1/management/invitations/<int:pk>/revoke/', management.InvitationRevoke.as_view()),
    path('api/v1/accounts/', views.AccountList.as_view()),
    path('api/v1/wishlist/', WishlistList.as_view()),
    path('api/v1/wishlist/state/', WishlistState.as_view()),
    path('api/v1/wishlist/<uuid:part_id>/', WishlistDetail.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/', AccountRequests.as_view()),
    path('api/v1/accounts/<uuid:account_id>/sent-requests/', ClientSentRequests.as_view()),
    path('api/v1/accounts/<uuid:account_id>/sent-requests/<uuid:pk>/', ClientSentRequestDetail.as_view()),
    path('api/v1/accounts/<uuid:account_id>/catalog/<uuid:part_id>/request-state/', ClientPartRequestState.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/', SupplierRequestDetail.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/review/', SupplierRequestReview.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/draft/', QuoteDraftView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/draft/discard/', QuoteDraftDiscard.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/draft/reprice/', QuoteDraftReprice.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/draft/assistant/', QuoteAssistantView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/draft/assistant/<uuid:run_id>/decisions/', QuoteAssistantDecisions.as_view()),
    path('api/v1/accounts/<uuid:account_id>/requests/<uuid:pk>/quotations/<uuid:quotation_id>/trace/', QuotationTrace.as_view()),
    path('api/v1/accounts/<uuid:account_id>/deals/<uuid:pk>/actions/', DealActions.as_view()),
    path('api/v1/accounts/<uuid:account_id>/deals/<uuid:pk>/messages/', DealMessages.as_view()),
    path('api/v1/accounts/<uuid:account_id>/pricing/settings/', PricingSettingsView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/pricing/history/', PricingHistory.as_view()),
    path('api/v1/accounts/<uuid:account_id>/price-lists/', PriceListsView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/price-lists/<uuid:pk>/', PriceListDetail.as_view()),
    path('api/v1/accounts/<uuid:account_id>/prices/', PricesView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/prices/export/', PricesExport.as_view()),
    path('api/v1/accounts/<uuid:account_id>/prices/import/', PriceImport.as_view()),
    path('api/v1/accounts/<uuid:account_id>/prices/import/template/', PriceImportTemplate.as_view()),
    path('api/v1/accounts/<uuid:account_id>/prices/import/jobs/<uuid:pk>/', PriceImportJobView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/prices/import/jobs/<uuid:pk>/errors/', PriceImportErrors.as_view()),
    path('api/v1/accounts/<uuid:account_id>/prices/<uuid:item_id>/history/', PriceHistory.as_view()),
    path('api/v1/accounts/<uuid:account_id>/clients/', PricingClients.as_view()),
    path('api/v1/accounts/<uuid:account_id>/clients/<uuid:client_id>/profile/', ClientProfileView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/pricing-rules/', PricingRulesView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/pricing-rules/<uuid:pk>/', PricingRuleDetail.as_view()),
    path('api/v1/accounts/<uuid:account_id>/pricing-rules/<uuid:pk>/archive/', PricingRuleArchiveView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/pricing/simulate/', PricingSimulate.as_view()),
    path('api/v1/catalog/', views.CatalogList.as_view()),
    path('api/v1/catalog/<uuid:pk>/technical/', CatalogTechnicalDetail.as_view()),
    path('api/v1/catalog/<uuid:part_id>/suppliers/', views.OffersView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/', views.InventoryList.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/ingest/', views.InventoryIngest.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/', SupplierInventoryImport.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/template/', SupplierInventoryImportTemplate.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/jobs/<uuid:pk>/', SupplierInventoryImportJobView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/jobs/<uuid:pk>/errors/', SupplierInventoryImportErrors.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/<uuid:item_id>/ledger/', views.LedgerList.as_view()),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema')),
]

if settings.DEBUG and not settings.DO_SPACES_KEY:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
