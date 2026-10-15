from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.throttling import AnonRateThrottle
from mall import basket, reorder, threads, deal_attachments, email_notifications, inbox, login_hints, management, push, views, whatsapp, whatsapp_accounts, whatsapp_verification
from mall.technical_api import (PartTypeList, PartTypeDetail, TemplateDetail, ApplicationList, ApplicationDetail,
                               ItemTechnicalDetail, CatalogTechnicalDetail)
from mall.matching_views import MatchingOverview, MatchingReview
from mall.brand_views import BrandAliasDetail, BrandAliases, BrandDetail, BrandList, GrupoDetail, GrupoList
from mall.case_views import CaseBatch, CaseDetail, CaseList
from mall.line_views import (InventorySeparate, LineDetail, LineList, LineMain, LineReferences, LineTransition, MergeDigest,
                             ReferenceDetail)
from mall.analytics import AnalyticsDashboard, UsageEvents
from mall.deal_admin import DealAdminCancel, DealAdminDetail, DealAdminList
from mall.apiag_connection import ApiagConnectionTest, ApiagConnectionView
from mall.apiag_sync_views import ApiagStatusView, ApiagSyncRequestView
from mall.inventory_history import (LedgerEntries, LedgerRun, LedgerRunChanges, LedgerRuns, ManagementLedgerEntries, ManagementLedgerRun,
                                    ManagementLedgerRunChanges, ManagementLedgerRuns)
from mall.sku_proposal_views import ProposalBulk, ProposalDetail, ProposalList, ProposalSummary
from mall.sku_review_views import ReviewDetail, ReviewList
from mall.wishlist import WishlistList, WishlistState, WishlistDetail
from mall.catalog_import import CatalogImport, CatalogImportJobView
from mall.catalog_classification import CatalogClassification
from mall.catalog_assistant import CatalogAssistantList, CatalogAssistantDetail
from mall.catalog_import_issues import CatalogImportIssueList, CatalogImportIssueDetail
from mall.catalog_grouping import CatalogGroupingMerge
from mall.catalog_split import CatalogMergedList, CatalogSplitView
from mall.catalog_oem_mark import CatalogCreateOEM, CatalogMarkOEM, CatalogUnmarkOEM
from mall.oem_lookup import CatalogOEMLookup
from mall.catalog_suffix_views import CompanySuffixes, SuffixDetail, SuffixList
from mall.oem_finder_views import OEMApplyHaltView, OEMRunDetail, OEMRunList, OEMRunRevert, OEMRunSpotCheck
from mall.oem_review_views import OEMReviewBulk, OEMReviewBulkStep, OEMReviewDecide, OEMReviewList, OEMReviewSummary
from mall.oem_auto_views import OEMPendingApply, OEMPendingList, OEMPendingPreview, OEMPendingSendToReview
from mall.oem_reference_views import (OEMReferenceBulkEdit, OEMReferenceCrossReference, OEMReferenceCrossReferences, OEMReferenceItem, OEMReferenceList,
                                     OEMReferencePartTypes, PartOEMEquivalents)
from mall.alternate_review import AlternateReviewItem, AlternateReviewList
from mall.catalog_media import CatalogImages, CatalogImageDetail, CatalogImageOrder
from mall.catalog_grouping_suggestions import CatalogGroupingList, CatalogGroupingClassify
from mall.supplier_import import SupplierInventoryImport, SupplierInventoryImportTemplate, SupplierInventoryImportJobView, SupplierInventoryImportErrors
from mall.request_views import AccountRequests, ClientSentRequests, ClientSentRequestSummary, ClientSentRequestDetail, ClientPartRequestState, ClientOpenUnits, ClientOpenUnitOrders, SupplierRequestDetail, SupplierRequestReview
from mall.deal_views import DealActions, DealMessages, PurchaseActivity
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
    path('api/v1/auth/password/', management.PasswordChangeView.as_view()),
    path('api/v1/auth/login-hint/', login_hints.LoginHint.as_view()),
    path('api/v1/analytics/events/', UsageEvents.as_view()),
    path('api/v1/management/analytics/', AnalyticsDashboard.as_view()),
    path('api/v1/management/deals/', DealAdminList.as_view()),
    path('api/v1/management/deals/<uuid:pk>/', DealAdminDetail.as_view()),
    path('api/v1/management/deals/<uuid:pk>/cancel/', DealAdminCancel.as_view()),
    path('api/v1/management/catalog/', management.CatalogList.as_view()),
    path('api/v1/management/catalog/proposals/', ProposalList.as_view()),
    path('api/v1/management/catalog/proposals/summary/', ProposalSummary.as_view()),
    path('api/v1/management/catalog/proposals/bulk/', ProposalBulk.as_view()),
    path('api/v1/management/catalog/proposals/review/', ReviewList.as_view()),
    path('api/v1/management/catalog/proposals/review/<uuid:pk>/', ReviewDetail.as_view()),
    path('api/v1/management/catalog/proposals/<uuid:pk>/', ProposalDetail.as_view()),
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
    path('api/v1/management/catalog/merged/', CatalogMergedList.as_view()),
    path('api/v1/management/catalog/mark-oem/', CatalogMarkOEM.as_view()),
    path('api/v1/management/catalog/create-oem/', CatalogCreateOEM.as_view()),
    path('api/v1/management/catalog/unmark-oem/', CatalogUnmarkOEM.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/split/', CatalogSplitView.as_view()),
    path('api/v1/management/catalog/import/', CatalogImport.as_view()),
    path('api/v1/management/catalog/import/issues/', CatalogImportIssueList.as_view()),
    path('api/v1/management/catalog/import/issues/<uuid:pk>/', CatalogImportIssueDetail.as_view()),
    path('api/v1/management/catalog/import/jobs/<uuid:pk>/', CatalogImportJobView.as_view()),
    path('api/v1/management/catalog/import/jobs/<uuid:pk>/classification/', CatalogClassification.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/oem-lookup/', CatalogOEMLookup.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/oem-equivalents/', PartOEMEquivalents.as_view()),
    path('api/v1/management/catalog/suffixes/', SuffixList.as_view()),
    path('api/v1/management/catalog/suffixes/<path:token>/', SuffixDetail.as_view()),
    path('api/v1/management/catalog/company-suffixes/', CompanySuffixes.as_view()),
    path('api/v1/management/catalog/taxonomy/', management.CatalogTaxonomy.as_view()),
    path('api/v1/management/catalog/bulk-edit/', management.CatalogBulkEdit.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/', management.CatalogDetail.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/images/', CatalogImages.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/images/order/', CatalogImageOrder.as_view()),
    path('api/v1/management/catalog/<uuid:pk>/images/<uuid:image_id>/', CatalogImageDetail.as_view()),
    path('api/v1/management/oem-finder/runs/', OEMRunList.as_view()),
    path('api/v1/management/oem-finder/runs/<int:pk>/', OEMRunDetail.as_view()),
    path('api/v1/management/oem-finder/runs/<int:pk>/spot-check/', OEMRunSpotCheck.as_view()),
    path('api/v1/management/oem-finder/runs/<int:pk>/revert/', OEMRunRevert.as_view()),
    path('api/v1/management/oem-finder/halt/', OEMApplyHaltView.as_view()),
    path('api/v1/management/oem-finder/pending/', OEMPendingList.as_view()),
    path('api/v1/management/oem-finder/pending/preview/', OEMPendingPreview.as_view()),
    path('api/v1/management/oem-finder/pending/apply/', OEMPendingApply.as_view()),
    path('api/v1/management/oem-finder/pending/send-to-review/', OEMPendingSendToReview.as_view()),
    path('api/v1/management/oem-review/', OEMReviewList.as_view()),
    path('api/v1/management/oem-review/summary/', OEMReviewSummary.as_view()),
    path('api/v1/management/oem-review/bulk/', OEMReviewBulk.as_view()),
    path('api/v1/management/oem-review/bulk/<int:pk>/', OEMReviewBulkStep.as_view()),
    path('api/v1/management/oem-review/<int:pk>/', OEMReviewDecide.as_view()),
    path('api/v1/management/oem-references/', OEMReferenceList.as_view()),
    path('api/v1/management/oem-references/part-types/', OEMReferencePartTypes.as_view()),
    path('api/v1/management/oem-references/bulk-edit/', OEMReferenceBulkEdit.as_view()),
    path('api/v1/management/oem-references/<uuid:pk>/', OEMReferenceItem.as_view()),
    path('api/v1/management/oem-references/<uuid:pk>/cross-references/', OEMReferenceCrossReferences.as_view()),
    path('api/v1/management/oem-references/<uuid:pk>/cross-references/<int:code_id>/', OEMReferenceCrossReference.as_view()),
    path('api/v1/management/matching/', MatchingOverview.as_view()),
    path('api/v1/management/matching/<uuid:pk>/', MatchingReview.as_view()),
    path('api/v1/management/inventory/', management.InventoryList.as_view()),
    path('api/v1/management/inventory/<uuid:pk>/', management.InventoryDetail.as_view()),
    path('api/v1/management/inventory/<uuid:pk>/separate/', InventorySeparate.as_view()),
    # The inventory refactor's S7 routes (OEM lines, References, cases, merges, brands, grupos); S8's screens read them.
    path('api/v1/management/lines/', LineList.as_view()),
    path('api/v1/management/lines/<uuid:pk>/', LineDetail.as_view()),
    path('api/v1/management/lines/<uuid:pk>/transition/', LineTransition.as_view()),
    path('api/v1/management/lines/<uuid:pk>/references/', LineReferences.as_view()),
    path('api/v1/management/lines/<uuid:pk>/main/', LineMain.as_view()),
    path('api/v1/management/references/<int:pk>/', ReferenceDetail.as_view()),
    path('api/v1/management/cases/', CaseList.as_view()),
    path('api/v1/management/cases/batches/<str:key>/', CaseBatch.as_view()),
    path('api/v1/management/cases/<uuid:pk>/', CaseDetail.as_view()),
    path('api/v1/management/merges/', MergeDigest.as_view()),
    path('api/v1/management/brands/', BrandList.as_view()),
    path('api/v1/management/brands/<int:pk>/', BrandDetail.as_view()),
    path('api/v1/management/brands/<int:pk>/aliases/', BrandAliases.as_view()),
    path('api/v1/management/brands/<int:pk>/aliases/<int:alias_id>/', BrandAliasDetail.as_view()),
    path('api/v1/management/grupos/', GrupoList.as_view()),
    path('api/v1/management/grupos/<uuid:pk>/', GrupoDetail.as_view()),
    path('api/v1/management/alternates/', management.AlternateList.as_view()),
    path('api/v1/management/alternate-review/', AlternateReviewList.as_view()),
    path('api/v1/management/alternate-review/<uuid:pk>/', AlternateReviewItem.as_view()),
    path('api/v1/management/alternates/<int:pk>/', management.AlternateDetail.as_view()),
    path('api/v1/management/accounts/', management.AccountList.as_view()),
    path('api/v1/management/accounts/<uuid:pk>/', management.AccountDetail.as_view()),
    path('api/v1/management/accounts/<uuid:pk>/apiag/', ApiagConnectionView.as_view()),
    path('api/v1/management/accounts/<uuid:pk>/apiag/test/', ApiagConnectionTest.as_view()),
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
    path('api/v1/accounts/<uuid:account_id>/basket/', basket.BasketView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/basket/activity/', basket.BasketActivity.as_view()),
    path('api/v1/accounts/<uuid:account_id>/purchase-activity/', PurchaseActivity.as_view()),
    path('api/v1/accounts/<uuid:account_id>/sent-requests/', ClientSentRequests.as_view()),
    path('api/v1/accounts/<uuid:account_id>/sent-requests/summary/', ClientSentRequestSummary.as_view()),
    path('api/v1/accounts/<uuid:account_id>/sent-requests/<uuid:pk>/', ClientSentRequestDetail.as_view()),
    path('api/v1/accounts/<uuid:account_id>/sent-requests/<uuid:pk>/missing/', reorder.OrderMissing.as_view()),
    path('api/v1/accounts/<uuid:account_id>/catalog/<uuid:part_id>/request-state/', ClientPartRequestState.as_view()),
    path('api/v1/accounts/<uuid:account_id>/open-units/', ClientOpenUnits.as_view()),
    path('api/v1/accounts/<uuid:account_id>/open-units/<uuid:part_id>/', ClientOpenUnitOrders.as_view()),
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
    path('api/v1/accounts/<uuid:account_id>/deals/<uuid:pk>/attachments/<int:attachment_id>/', deal_attachments.DealAttachmentFile.as_view()),
    path('api/v1/accounts/<uuid:account_id>/threads/', threads.Threads.as_view()),
    path('api/v1/accounts/<uuid:account_id>/threads/<uuid:thread_id>/messages/', threads.ThreadMessages.as_view()),
    path('api/v1/accounts/<uuid:account_id>/threads/<uuid:thread_id>/orders/', threads.ThreadOrders.as_view()),
    path('api/v1/accounts/<uuid:account_id>/threads/<uuid:thread_id>/attachments/<int:attachment_id>/', deal_attachments.DealAttachmentFile.as_view()),
    path('api/v1/accounts/<uuid:account_id>/threads/<uuid:thread_id>/attachments/<int:attachment_id>/retry/', deal_attachments.DealAttachmentRetry.as_view()),
    path('api/v1/accounts/<uuid:account_id>/deals/<uuid:pk>/attachments/<int:attachment_id>/retry/', deal_attachments.DealAttachmentRetry.as_view()),
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
    path('api/v1/catalog/suggest/', views.CatalogSuggest.as_view()),
    path('api/v1/inbox/', inbox.InboxSummary.as_view()),
    path('api/v1/inbox/conversations/', inbox.InboxConversations.as_view()),
    path('api/v1/inbox/conversations/<uuid:pk>/read/', inbox.InboxConversationRead.as_view()),
    path('api/v1/inbox/conversations/<uuid:pk>/place/', inbox.InboxConversationPlace.as_view()),
    path('api/v1/inbox/groups/', inbox.InboxGroups.as_view()),
    path('api/v1/inbox/groups/<uuid:pk>/', inbox.InboxGroupDetail.as_view()),
    path('api/v1/inbox/notifications/', inbox.InboxNotifications.as_view()),
    path('api/v1/inbox/notifications/seen/', inbox.InboxNotificationsSeen.as_view()),
    path('api/v1/inbox/push/', push.InboxPush.as_view()),
    path('api/v1/inbox/push/remove/', push.InboxPushRemove.as_view()),
    path('api/v1/inbox/email/', email_notifications.InboxEmail.as_view()),
    path('api/v1/inbox/whatsapp/', whatsapp.InboxWhatsApp.as_view()),
    path('api/v1/inbox/whatsapp/verify/', whatsapp_verification.InboxWhatsAppVerify.as_view()),
    path('api/v1/accounts/<uuid:account_id>/whatsapp/', whatsapp_accounts.AccountWhatsAppView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/whatsapp/verify/', whatsapp_accounts.AccountWhatsAppVerify.as_view()),
    path('api/v1/whatsapp/webhook/', whatsapp.WhatsAppWebhook.as_view()),
    path('api/v1/catalog/<uuid:pk>/technical/', CatalogTechnicalDetail.as_view()),
    path('api/v1/catalog/<uuid:part_id>/suppliers/', views.OffersView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/apiag/', ApiagStatusView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/apiag/sync/', ApiagSyncRequestView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/', views.InventoryList.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/ingest/', views.InventoryIngest.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/', SupplierInventoryImport.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/template/', SupplierInventoryImportTemplate.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/jobs/<uuid:pk>/', SupplierInventoryImportJobView.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/import/jobs/<uuid:pk>/errors/', SupplierInventoryImportErrors.as_view()),
    path('api/v1/accounts/<uuid:account_id>/inventory/<uuid:item_id>/ledger/', views.LedgerList.as_view()),
    path('api/v1/accounts/<uuid:account_id>/ledger/', LedgerEntries.as_view()),
    path('api/v1/accounts/<uuid:account_id>/ledger/runs/', LedgerRuns.as_view()),
    path('api/v1/accounts/<uuid:account_id>/ledger/runs/<uuid:run_id>/', LedgerRun.as_view()),
    path('api/v1/accounts/<uuid:account_id>/ledger/runs/<uuid:run_id>/changes/', LedgerRunChanges.as_view()),
    path('api/v1/management/accounts/<uuid:account_id>/ledger/', ManagementLedgerEntries.as_view()),
    path('api/v1/management/accounts/<uuid:account_id>/ledger/runs/', ManagementLedgerRuns.as_view()),
    path('api/v1/management/accounts/<uuid:account_id>/ledger/runs/<uuid:run_id>/', ManagementLedgerRun.as_view()),
    path('api/v1/management/accounts/<uuid:account_id>/ledger/runs/<uuid:run_id>/changes/', ManagementLedgerRunChanges.as_view()),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema')),
]

if settings.DEBUG and not settings.DO_SPACES_KEY:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
